"""
Azure OpenAI provider for ShipMate agents.

Implements the IDENTICAL `invoke_structured(...)` contract as
`BedrockProvider`, using Azure OpenAI's chat-completions function/tool calling
to force structured JSON output. The model is required to call a single
function whose `parameters` are the Pydantic JSON schema; the resulting
tool-call carries the validated JSON arguments.

Why function-calling with tool_choice (vs response_format / "give me JSON"):
  • `tool_choice={"type":"function","function":{"name":...}}` FORCES the model
    to emit exactly one tool call matching the schema — the same guarantee
    Bedrock's `toolChoice={"tool": ...}` gives us. No markdown extraction, no
    regex, no JSON-mode prompting.
  • Azure accepts Pydantic's `$defs`/`$ref` nested schemas directly (verified
    against CoderOutput), so we pass `model_json_schema()` unchanged — same as
    Bedrock.
  • The openai SDK is sync; we wrap calls with `asyncio.to_thread` exactly like
    BedrockProvider does, so multi-agent fan-out stays non-blocking.

Auth: API key + endpoint from env (no credential rollover — this is the
default, hosted production path on Azure). Selected by the factory in
`llm_provider.py` when SHIPMATE_LLM_PROVIDER=azure (the default).

Env keys:
  AZURE_OPENAI_ENDPOINT          https://<resource>.openai.azure.com/
  AZURE_OPENAI_API_KEY           account key1
  AZURE_OPENAI_API_VERSION       e.g. 2024-10-21 (default below)
  AZURE_OPENAI_DEPLOYMENT_SMART  deployment name for the "smart" model
  AZURE_OPENAI_DEPLOYMENT_FAST   deployment name for the "fast" model
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

logger = logging.getLogger("shipmate.azure_openai_provider")

_DEFAULT_API_VERSION = "2024-10-21"


class AzureOpenAIProvider:
    """Azure OpenAI chat-completions client returning Pydantic-validated objects."""

    def __init__(self) -> None:
        # Defer the import so a missing `openai` dep doesn't break the rest of
        # the app at import time — only when the Azure path is actually selected.
        from openai import AzureOpenAI

        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
        api_key = os.getenv("AZURE_OPENAI_API_KEY")
        if not endpoint or not api_key:
            raise RuntimeError(
                "AzureOpenAIProvider requires AZURE_OPENAI_ENDPOINT and "
                "AZURE_OPENAI_API_KEY. Set them in the environment (see "
                ".env.example) or switch SHIPMATE_LLM_PROVIDER back to bedrock."
            )

        api_version = os.getenv("AZURE_OPENAI_API_VERSION", _DEFAULT_API_VERSION)
        # Match Bedrock's generous read timeout: a full CoderOutput / planner
        # schema can take a while. The SDK retries network errors itself; we
        # cap to 1 here so a transient failure surfaces to the agent-layer
        # fallback rather than silently multiplying wall time.
        self.client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
            timeout=300.0,
            max_retries=1,
        )

        # Deployment NAMES (not model names) — these are what you typed when you
        # created the deployment in the Azure OpenAI resource. Defaults match
        # the names provisioned for ShipMate (shipmate-smart / shipmate-fast).
        self.smart_deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_SMART", "shipmate-smart")
        self.fast_deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_FAST", "shipmate-fast")
        # Back-compat: the older single-deployment key from .env.example. If the
        # smart deployment env is unset but the legacy one is present, use it.
        legacy = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME")
        if legacy and not os.getenv("AZURE_OPENAI_DEPLOYMENT_SMART"):
            self.smart_deployment = legacy
        logger.info(
            "AzureOpenAIProvider initialized (endpoint=%s, api_version=%s, "
            "smart=%s, fast=%s)",
            endpoint, api_version, self.smart_deployment, self.fast_deployment,
        )

    def invoke_structured_sync(
        self,
        system_prompt: str,
        user_prompt: str,
        schema_class: Type[BaseModel],
        deployment_hint: Literal["smart", "fast"] = "smart",
    ) -> BaseModel:
        """Synchronous structured-output call. Safe from inside a running event
        loop (the openai SDK call is blocking but we don't await anything).

        Includes a one-shot retry if Pydantic validation fails (re-prompts with
        an explicit array-not-string reminder), plus the CoderOutput
        short-summary retry. Mirrors BedrockProvider.invoke_structured_sync."""
        deployment = self.smart_deployment if deployment_hint == "smart" else self.fast_deployment
        tool_name = f"emit_{schema_class.__name__}"
        schema = schema_class.model_json_schema()
        max_tokens = 8192

        with run_trace.span(
            component=f"llm:azure:{deployment_hint}",
            op=f"invoke:{schema_class.__name__}",
            model=deployment, prompt=system_prompt + user_prompt,
        ) as _sp:
            try:
                payload = self._call_chat(
                    deployment, tool_name, schema, system_prompt, user_prompt, max_tokens
                )
                # Schema-aware coercion fixes a stringified array/object up front
                # so the validation-retry below is a rare fallback, not the main path.
                coerced = _coerce_to_schema(payload, schema_class)
                result = schema_class.model_validate(coerced)
                result = self._maybe_retry_short_summary(
                    result, schema_class, deployment, tool_name, schema,
                    system_prompt, user_prompt, max_tokens, span=_sp,
                )
                _sp.set_output(result.model_dump_json() if hasattr(result, "model_dump_json") else result)
                return result
            except Exception as first_err:
                from pydantic import ValidationError as _VE

                err_str = str(first_err)
                is_validation = isinstance(first_err, _VE) or "validation error" in err_str.lower()
                if not is_validation:
                    # Unlike Bedrock there's no ADA cred-rollover to recover from —
                    # an auth error here means a bad/rotated API key, which a retry
                    # won't fix. Surface it.
                    raise
                logger.warning(
                    "AzureOpenAIProvider: structured-output validation failed (%s); "
                    "retrying once with explicit array-not-string reminder",
                    err_str[:200],
                )
                _sp.bump_retry()
                retry_user = (
                    user_prompt
                    + "\n\n# RETRY NOTICE\nA prior attempt returned a list/object "
                    "field (e.g. `files`) as a JSON-encoded STRING instead of a "
                    "native JSON array/object, which broke parsing. Return every "
                    "field as a native JSON value matching the function schema "
                    "exactly. Do not stringify arrays or nested objects."
                )
                payload = self._call_chat(
                    deployment, tool_name, schema, system_prompt, retry_user, max_tokens
                )
                coerced = _coerce_to_schema(payload, schema_class)
                return schema_class.model_validate(coerced)

    # Thin back-compat shim — the real predicate lives in provider_coercion.
    _is_short_summary_coder_output = staticmethod(_is_short_summary)

    def _maybe_retry_short_summary(
        self,
        result: BaseModel,
        schema_class: Type[BaseModel],
        deployment: str,
        tool_name: str,
        schema: dict,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        span: Any = None,
    ) -> BaseModel:
        """One-shot retry for a CoderOutput with files but a near-empty summary.
        Shrinks oversized file blocks to free output budget, re-invokes once,
        keeps whichever has the longer summary. Mirrors BedrockProvider."""
        if not _is_short_summary(result):
            return result
        if span is not None:
            span.bump_retry()
        logger.warning(
            "AzureOpenAIProvider: CoderOutput summary suspiciously short "
            "(%d chars, %d files) — retrying once with shrunk context",
            len(getattr(result, "summary", "") or ""),
            len(getattr(result, "files", []) or []),
        )
        shrunk_user = _shrink_oversized_file_blocks(user_prompt)
        try:
            payload = self._call_chat(
                deployment, tool_name, schema, system_prompt, shrunk_user, max_tokens
            )
            coerced = _coerce_to_schema(payload, schema_class)
            retried = schema_class.model_validate(coerced)
        except Exception as e:
            logger.info("short-summary retry failed (%s); keeping first result", e)
            return result
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
        explicit fix-up notice listing the issues. Reuses the full
        invoke_structured_sync path. Identical semantics to BedrockProvider."""
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
            "AzureOpenAIProvider: re-invoking with lint feedback (%d issue(s))",
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

    def _call_chat(
        self,
        deployment: str,
        tool_name: str,
        schema: dict,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        """Single forced-tool-call round-trip. Returns the parsed tool-call
        arguments dict. Raises if the model emitted no tool call."""
        resp = self.client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=max_tokens,
            temperature=0.2,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": (
                            f"Emit a {tool_name} JSON object matching the "
                            f"provided schema."
                        ),
                        "parameters": schema,
                    },
                }
            ],
            # Force the model to call the tool — guarantees structured output,
            # the Azure equivalent of Bedrock's toolChoice={"tool": ...}.
            tool_choice={"type": "function", "function": {"name": tool_name}},
        )

        choice = resp.choices[0]
        tool_calls = getattr(choice.message, "tool_calls", None)
        if not tool_calls:
            raise RuntimeError(
                f"Azure OpenAI response contained no tool call for {tool_name}; "
                f"finish_reason={choice.finish_reason!r}"
            )
        raw_args = tool_calls[0].function.arguments
        try:
            return json.loads(raw_args)
        except (json.JSONDecodeError, TypeError) as e:
            raise RuntimeError(
                f"Azure OpenAI tool-call arguments for {tool_name} were not "
                f"valid JSON: {e}"
            ) from e
