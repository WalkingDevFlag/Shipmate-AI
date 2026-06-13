"""
AWS Bedrock provider for ShipMate agents.

Implements the same `invoke_structured(...)` contract as
`AzureOpenAIProvider`, using Bedrock's Converse API with `toolConfig` to
force structured JSON output. The model is required to call a single tool
whose `inputSchema` is the Pydantic JSON schema; the resulting tool-use
block carries the validated JSON payload.

Why Converse + toolConfig (vs InvokeModel + per-model body):
  • Converse is a unified API across Anthropic / Llama / Mistral / Titan.
  • toolChoice={"tool": ...} guarantees the model emits structured JSON
    matching the schema — no markdown extraction, no regex, no "give me
    valid JSON" prompting.
  • boto3-only — no extra deps. The SDK is sync; we wrap calls with
    `asyncio.to_thread` exactly like AzureOpenAIProvider does.

Auth: relies on the standard boto3 credential provider chain — environment
variables, the shared config file at ~/.aws/credentials, or an instance/role
profile. boto3 resolves whichever is present transparently; no AWS_PROFILE
env var is required.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Literal, Type

from pydantic import BaseModel

from app.services import run_trace
from app.services.provider_coercion import (
    MIN_CODER_SUMMARY_CHARS as _MIN_CODER_SUMMARY_CHARS,
    coerce_to_schema as _coerce_to_schema,
    is_short_summary_coder_output as _is_short_summary,
    shrink_oversized_file_blocks as _shrink_oversized_file_blocks,
)

logger = logging.getLogger("shipmate.bedrock_provider")


# Model generations that removed the sampling params (temperature/top_p/top_k):
# Fable 5 and Opus 4.7+. Passing `temperature` to these via Converse returns a
# 400 ValidationException. Match against the bare model name inside the
# inference-profile id (e.g. "us.anthropic.claude-fable-5",
# "global.anthropic.claude-opus-4-8") so region/profile prefixes don't matter.
_NO_TEMPERATURE_MODELS = ("claude-fable-5", "claude-opus-4-8", "claude-opus-4-7")


def _accepts_temperature(model_id: str) -> bool:
    """True if the model still accepts an explicit `temperature`. Fable 5 and
    Opus 4.7/4.8 reject it (400); everything older accepts it."""
    mid = (model_id or "").lower()
    return not any(tag in mid for tag in _NO_TEMPERATURE_MODELS)


# Models that reject Converse forced tool use (`toolChoice={"tool": ...}`) with
# a ValidationException. Fable 5 / Mythos 5 require the native structured-output
# path (`outputConfig.textFormat` + JSON schema) instead, which returns the JSON
# in a text block rather than a toolUse block.
_NO_FORCED_TOOL_USE_MODELS = ("claude-fable-5", "claude-mythos-5")


def _supports_forced_tool_use(model_id: str) -> bool:
    """True if the model supports `toolChoice={"tool": ...}` to force structured
    output. Fable 5 / Mythos 5 do not — use native structured outputs there."""
    mid = (model_id or "").lower()
    return not any(tag in mid for tag in _NO_FORCED_TOOL_USE_MODELS)


class BedrockProvider:
    """Bedrock Converse-API client returning Pydantic-validated objects."""

    def __init__(self) -> None:
        # Defer the boto3 import so a missing dep doesn't break the rest of
        # the app — the factory in llm_provider.py catches and falls back.
        import boto3

        from botocore.config import Config

        region = os.getenv("AWS_REGION", "us-west-2")
        # Default boto3 read timeout is 60s — Sonnet 4.6 producing a full
        # 4-list-of-objects schema (planner / repo_analyst) can exceed that.
        # Push to 5min and disable retries (we already have an outer fallback
        # at the agent layer; retrying inside boto3 just multiplies wall time).
        self.client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(read_timeout=300, connect_timeout=10, retries={"max_attempts": 1}),
        )
        # Defaults use cross-region inference profile IDs (`us.*`). The newer
        # Claude generation (Sonnet 4.6, Haiku 4.5) doesn't support direct
        # on-demand throughput — Bedrock requires invocation through an
        # inference profile so the request can route across regions.
        self.smart_model = os.getenv(
            "BEDROCK_MODEL_SMART",
            "us.anthropic.claude-sonnet-4-6",
        )
        self.fast_model = os.getenv(
            "BEDROCK_MODEL_FAST",
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        )
        logger.info(
            "BedrockProvider initialized (region=%s, smart=%s, fast=%s)",
            region, self.smart_model, self.fast_model,
        )

    def invoke_structured_sync(
        self,
        system_prompt: str,
        user_prompt: str,
        schema_class: Type[BaseModel],
        deployment_hint: Literal["smart", "fast"] = "smart",
    ) -> BaseModel:
        """Synchronous version. Safe to call from inside a running event loop
        (e.g. from a FastAPI request handler). The boto3 SDK is sync; we just
        skip the asyncio wrapping. Use this from `LLMService.enhance`.

        Includes a one-shot retry if Pydantic validation fails. Bedrock
        occasionally serialises nested complex fields (`files: [...]`) as a
        string with escape sequences that don't round-trip through json.loads
        — when that happens we re-prompt with an explicit reminder that the
        field must be a native JSON array, not a stringified one.
        """
        model_id = self.smart_model if deployment_hint == "smart" else self.fast_model
        tool_name = f"emit_{schema_class.__name__}"
        schema = schema_class.model_json_schema()
        max_tokens = 8192

        # One trace span per logical structured call. Retries inside this method
        # bump the span's counter, so a row with retries>0 is the validation /
        # auth / short-summary waste that used to be invisible in the logs.
        with run_trace.span(
            component=f"llm:bedrock:{deployment_hint}",
            op=f"invoke:{schema_class.__name__}",
            model=model_id, prompt=system_prompt + user_prompt,
        ) as _sp:
            try:
                payload = self._call_converse(
                    model_id, tool_name, schema, system_prompt, user_prompt, max_tokens
                )
                # Schema-aware coercion repairs the common Bedrock quirk (a nested
                # array/object returned as a JSON STRING) deterministically up
                # front, so the LLM validation-retry below only fires for
                # genuinely malformed output, not for every stringified array.
                coerced = _coerce_to_schema(payload, schema_class)
                result = schema_class.model_validate(coerced)
                # Flaky-output guard: a CoderOutput that has files but an almost
                # empty summary is a sign the model truncated. Retry once with a
                # shrunk context (drop the back half of oversized target-file
                # blocks so the model has more budget for its reasoning).
                result = self._maybe_retry_short_summary(
                    result, schema_class, model_id, tool_name, schema,
                    system_prompt, user_prompt, max_tokens, span=_sp,
                )
                _sp.set_output(result.model_dump_json() if hasattr(result, "model_dump_json") else result)
                return result
            except Exception as first_err:
                from pydantic import ValidationError as _VE
                err_str = str(first_err)
                # Auto-recover from expired creds: rebuild the boto3 client on
                # auth-class errors so the next call picks up freshly-refreshed
                # ADA creds without needing a process restart. The default
                # session caches credential providers, so simply discarding the
                # client and rebuilding it is what triggers the refresh.
                if any(m in err_str for m in (
                    "ExpiredToken", "ExpiredTokenException",
                    "InvalidSignatureException", "UnrecognizedClientException",
                    "Signature expired",
                )):
                    logger.warning(
                        "BedrockProvider: auth failure (%s) — rebuilding boto3 "
                        "client to pick up refreshed credentials, then retrying once",
                        type(first_err).__name__,
                    )
                    _sp.bump_retry()
                    import boto3
                    from botocore.config import Config
                    self.client = boto3.client(
                        "bedrock-runtime",
                        region_name=os.getenv("AWS_REGION", "us-west-2"),
                        config=Config(read_timeout=300, connect_timeout=10,
                                      retries={"max_attempts": 1}),
                    )
                    payload = self._call_converse(
                        model_id, tool_name, schema, system_prompt, user_prompt, max_tokens
                    )
                    coerced = _coerce_to_schema(payload, schema_class)
                    result = schema_class.model_validate(coerced)
                    return self._maybe_retry_short_summary(
                        result, schema_class, model_id, tool_name, schema,
                        system_prompt, user_prompt, max_tokens, span=_sp,
                    )

                is_validation = isinstance(first_err, _VE) or "validation error" in err_str.lower()
                if not is_validation:
                    raise
                logger.warning(
                    "BedrockProvider: structured-output validation failed (%s); "
                    "retrying once with explicit array-not-string reminder",
                    err_str[:200],
                )
                _sp.bump_retry()
                retry_user = (
                    user_prompt
                    + "\n\n# RETRY NOTICE\nA prior attempt returned the `files` field "
                    "as a JSON-encoded STRING instead of a native JSON array, which "
                    "broke parsing. Return `files` as a native JSON array of objects "
                    "matching the tool schema exactly (each object's fields as "
                    "native values). Do not stringify the array or any nested object."
                )
                payload = self._call_converse(
                    model_id, tool_name, schema, system_prompt, retry_user, max_tokens
                )
                coerced = _coerce_to_schema(payload, schema_class)
                return schema_class.model_validate(coerced)

    # Thin back-compat shim — the real predicate lives in provider_coercion.
    _is_short_summary_coder_output = staticmethod(_is_short_summary)

    def _maybe_retry_short_summary(
        self,
        result: BaseModel,
        schema_class: Type[BaseModel],
        model_id: str,
        tool_name: str,
        schema: dict,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        span: Any = None,
    ) -> BaseModel:
        """One-shot retry when a CoderOutput came back with files but a
        near-empty summary. Shrinks oversized target-file blocks in the
        prompt to free token budget, then re-invokes once. If the retry is
        ALSO short, keep whichever has the longer summary (never worse)."""
        if not _is_short_summary(result):
            return result
        if span is not None:
            span.bump_retry()
        logger.warning(
            "BedrockProvider: CoderOutput summary suspiciously short "
            "(%d chars, %d files) — retrying once with shrunk context",
            len(getattr(result, "summary", "") or ""),
            len(getattr(result, "files", []) or []),
        )
        shrunk_user = _shrink_oversized_file_blocks(user_prompt)
        try:
            payload = self._call_converse(
                model_id, tool_name, schema, system_prompt, shrunk_user, max_tokens
            )
            coerced = _coerce_to_schema(payload, schema_class)
            retried = schema_class.model_validate(coerced)
        except Exception as e:
            logger.info("short-summary retry failed (%s); keeping first result", e)
            return result
        # Prefer whichever summary is longer — the retry isn't guaranteed better.
        first_len = len(getattr(result, "summary", "") or "")
        retry_len = len(getattr(retried, "summary", "") or "")
        return retried if retry_len >= first_len else result

    def invoke_with_lint_feedback(
        self,
        system_prompt: str,
        user_prompt: str,
        schema_class: Type[BaseModel],
        lint_issues: list[str],
        deployment_hint: Literal["smart", "fast"] = "smart",
    ) -> BaseModel:
        """Re-invoke after a patch was rejected by post-Coder lint. Appends an
        explicit fix-up notice listing the issues so the model corrects them
        rather than re-emitting the same mistake. Reuses the full
        invoke_structured_sync path (auth-retry, validation-retry,
        short-summary retry all still apply)."""
        issues_block = "\n".join(f"  - {i}" for i in lint_issues)
        feedback_user = (
            user_prompt
            + "\n\n# LINT REJECTION — CORRECT AND RESUBMIT\n"
            "Your previous patch was rejected by automated lint for:\n"
            f"{issues_block}\n"
            "Produce a corrected patch that resolves every issue above. Do not "
            "reintroduce them. If an issue was a hallucinated import or symbol, "
            "either add the missing definition to a file in `files` or remove "
            "the reference. Return ONLY the structured object."
        )
        logger.info(
            "BedrockProvider: re-invoking with lint feedback (%d issue(s))",
            len(lint_issues),
        )
        return self.invoke_structured_sync(
            system_prompt, feedback_user, schema_class, deployment_hint,
        )

    async def invoke_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        schema_class: Type[BaseModel],
        deployment_hint: Literal["smart", "fast"] = "smart",
    ) -> BaseModel:
        """Async wrapper — kept for parallel multi-agent fan-out. Calls the
        sync implementation on a worker thread so we don't block the loop."""
        return await asyncio.to_thread(
            self.invoke_structured_sync,
            system_prompt, user_prompt, schema_class, deployment_hint,
        )

    def _call_converse(
        self,
        model_id: str,
        tool_name: str,
        schema: dict,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        def _call() -> dict[str, Any]:
            # Fable 5 and Opus 4.7/4.8 removed the sampling params — passing
            # `temperature` returns a 400 (ValidationException). Omit it for
            # those generations; keep the deterministic 0.2 on models that
            # still accept it (Sonnet 4.6, Haiku 4.5, Opus ≤4.6).
            inference_config: dict[str, Any] = {"maxTokens": max_tokens}
            if _accepts_temperature(model_id):
                inference_config["temperature"] = 0.2

            # Fable 5 / Mythos 5 reject forced tool use; use the native
            # structured-output path (outputConfig.textFormat). All other models
            # keep the proven toolConfig route.
            if not _supports_forced_tool_use(model_id):
                return self._call_converse_native(
                    model_id, tool_name, schema, system_prompt,
                    user_prompt, inference_config,
                )

            resp = self.client.converse(
                modelId=model_id,
                system=[{"text": system_prompt}],
                messages=[
                    {"role": "user", "content": [{"text": user_prompt}]},
                ],
                inferenceConfig=inference_config,
                toolConfig={
                    "tools": [
                        {
                            "toolSpec": {
                                "name": tool_name,
                                "description": (
                                    f"Emit a {tool_name} JSON object "
                                    f"matching the provided schema."
                                ),
                                "inputSchema": {"json": schema},
                            }
                        }
                    ],
                    # Force the model to call the tool — guarantees structured output.
                    "toolChoice": {"tool": {"name": tool_name}},
                },
            )

            for block in resp.get("output", {}).get("message", {}).get("content", []):
                if "toolUse" in block:
                    payload = block["toolUse"].get("input")
                    if payload is None:
                        raise RuntimeError(
                            f"Bedrock toolUse block had no `input` for {tool_name}"
                        )
                    return payload

            raise RuntimeError(
                f"Bedrock response contained no toolUse block for {tool_name}; "
                f"stopReason={resp.get('stopReason')!r}"
            )

        return _call()

    def _call_converse_native(
        self,
        model_id: str,
        tool_name: str,
        schema: dict,
        system_prompt: str,
        user_prompt: str,
        inference_config: dict[str, Any],
    ) -> dict[str, Any]:
        """Structured output via Converse `outputConfig.textFormat` (JSON schema).

        Fable 5 / Mythos 5 reject `toolChoice={"tool": ...}`, so we constrain the
        response format directly. The model returns the schema-conforming JSON in
        a normal text block (no toolUse block); we parse it with json.loads."""
        resp = self.client.converse(
            modelId=model_id,
            system=[{"text": system_prompt}],
            messages=[
                {"role": "user", "content": [{"text": user_prompt}]},
            ],
            inferenceConfig=inference_config,
            outputConfig={
                "textFormat": {
                    "type": "json_schema",
                    "structure": {
                        "jsonSchema": {
                            "name": tool_name,
                            # Unlike toolConfig's inputSchema.json (a dict), this
                            # field wants the schema as a JSON-encoded string.
                            "schema": json.dumps(schema),
                        }
                    },
                }
            },
        )

        for block in resp.get("output", {}).get("message", {}).get("content", []):
            text = block.get("text")
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError as e:
                    raise RuntimeError(
                        f"Fable native structured output was not valid JSON for "
                        f"{tool_name}: {e}; head={text[:200]!r}"
                    )

        raise RuntimeError(
            f"Bedrock native-format response had no text block for {tool_name}; "
            f"stopReason={resp.get('stopReason')!r}"
        )
