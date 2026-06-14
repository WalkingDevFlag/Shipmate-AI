"""Unit tests for AzureOpenAIProvider + the llm_provider factory.

All offline — no `openai` network calls. We build the provider via __new__ and
patch `_call_chat` (the single forced-tool-call round-trip), exactly mirroring
test_bedrock_retry_triggers.py so the two providers stay behaviourally aligned.

Covers:
  • Factory selection: default=azure, SHIPMATE_LLM_PROVIDER wins over legacy
    LLM_PROVIDER, invalid value falls back to azure, reset clears the cache.
  • Provider contract parity: short-summary retry, lint-feedback re-prompt,
    validation-error retry, deployment_hint -> smart/fast deployment routing.
"""
from unittest.mock import patch

import pytest

from app.agents.coder_agent import CoderOutput
from app.services.azure_openai_provider import (
    AzureOpenAIProvider,
    _MIN_CODER_SUMMARY_CHARS,
    _shrink_oversized_file_blocks,
)


def _provider() -> AzureOpenAIProvider:
    """A provider with deployment names set but no openai client (we patch
    _call_chat, so the AzureOpenAI client is never constructed)."""
    p = AzureOpenAIProvider.__new__(AzureOpenAIProvider)
    p.smart_deployment = "smart-test"
    p.fast_deployment = "fast-test"
    return p


def _coder_payload(summary: str):
    return {
        "files": [{"path": "a.py", "new_content": "x = 1", "rationale": "r"}],
        "summary": summary,
        "skipped": [],
    }


# ── Factory ──────────────────────────────────────────────────────────────────

class TestFactorySelection:
    def test_default_is_azure(self, monkeypatch):
        from app.services import llm_provider
        monkeypatch.delenv("SHIPMATE_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        assert llm_provider.provider_kind() == "azure"

    def test_legacy_bedrock_still_selectable(self, monkeypatch):
        # Bedrock remains an explicit opt-in even though azure is now default.
        from app.services import llm_provider
        monkeypatch.delenv("SHIPMATE_LLM_PROVIDER", raising=False)
        monkeypatch.setenv("LLM_PROVIDER", "bedrock")
        assert llm_provider.provider_kind() == "bedrock"

    def test_legacy_llm_provider_honoured(self, monkeypatch):
        from app.services import llm_provider
        monkeypatch.delenv("SHIPMATE_LLM_PROVIDER", raising=False)
        monkeypatch.setenv("LLM_PROVIDER", "azure")
        assert llm_provider.provider_kind() == "azure"

    def test_shipmate_key_wins_over_legacy(self, monkeypatch):
        from app.services import llm_provider
        monkeypatch.setenv("SHIPMATE_LLM_PROVIDER", "azure")
        monkeypatch.setenv("LLM_PROVIDER", "bedrock")
        assert llm_provider.provider_kind() == "azure"

    def test_invalid_value_falls_back_to_azure(self, monkeypatch):
        from app.services import llm_provider
        monkeypatch.setenv("SHIPMATE_LLM_PROVIDER", "gpt5pro")
        assert llm_provider.provider_kind() == "azure"

    def test_reset_clears_cache(self, monkeypatch):
        from app.services import llm_provider
        # Force azure, build a fake-cached provider, reset, confirm cleared.
        monkeypatch.setenv("SHIPMATE_LLM_PROVIDER", "azure")
        llm_provider._provider = object()
        llm_provider._provider_kind = "azure"
        llm_provider.reset_provider()
        assert llm_provider._provider is None
        assert llm_provider._provider_kind is None


class TestProviderConstructionRequiresCreds:
    def test_missing_endpoint_raises(self, monkeypatch):
        monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
        monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="AZURE_OPENAI_ENDPOINT"):
            AzureOpenAIProvider()


# ── Helpers parity ─────────────────────────────────────────────────────────────

class TestShrinkOversizedFileBlocks:
    def test_short_prompt_unchanged(self):
        s = "x" * 100
        assert _shrink_oversized_file_blocks(s) == s

    def test_big_prompt_shrunk_keeping_head_and_tail(self):
        big = "HEAD" + ("y" * 20_000) + "TAIL"
        out = _shrink_oversized_file_blocks(big, threshold=8_000)
        assert len(out) < len(big)
        assert out.startswith("HEAD")
        assert out.endswith("TAIL")
        assert "dropped" in out


class TestShortSummaryDetection:
    def test_short_summary_with_files_is_flaky(self):
        co = CoderOutput(
            files=[{"path": "a.py", "new_content": "x", "rationale": "r"}],
            summary="fix",
        )
        assert AzureOpenAIProvider._is_short_summary_coder_output(co) is True

    def test_long_summary_is_not_flaky(self):
        co = CoderOutput(
            files=[{"path": "a.py", "new_content": "x", "rationale": "r"}],
            summary="A sufficiently detailed summary of the patch and its rationale.",
        )
        assert AzureOpenAIProvider._is_short_summary_coder_output(co) is False

    def test_threshold_matches_bedrock(self):
        from app.services.bedrock_provider import (
            _MIN_CODER_SUMMARY_CHARS as bedrock_min,
        )
        assert _MIN_CODER_SUMMARY_CHARS == bedrock_min == 30


# ── Contract parity (patching _call_chat) ──────────────────────────────────────

class TestDeploymentRouting:
    def test_smart_hint_uses_smart_deployment(self):
        p = _provider()
        seen = {}

        def fake(deployment, tool_name, schema, system_prompt, user_prompt, max_tokens):
            seen["deployment"] = deployment
            return _coder_payload("A nice long and complete summary of the patch.")

        with patch.object(p, "_call_chat", side_effect=fake):
            p.invoke_structured_sync("sys", "u", CoderOutput, "smart")
        assert seen["deployment"] == "smart-test"

    def test_fast_hint_uses_fast_deployment(self):
        p = _provider()
        seen = {}

        def fake(deployment, tool_name, schema, system_prompt, user_prompt, max_tokens):
            seen["deployment"] = deployment
            return _coder_payload("A nice long and complete summary of the patch.")

        with patch.object(p, "_call_chat", side_effect=fake):
            p.invoke_structured_sync("sys", "u", CoderOutput, "fast")
        assert seen["deployment"] == "fast-test"


class TestShortSummaryRetry:
    def test_retry_fires_and_keeps_longer_summary(self):
        p = _provider()
        calls = []

        def fake(deployment, tool_name, schema, system_prompt, user_prompt, max_tokens):
            calls.append(user_prompt)
            if len(calls) == 1:
                return _coder_payload("fix")  # short -> triggers retry
            return _coder_payload("A properly detailed corrective summary.")

        with patch.object(p, "_call_chat", side_effect=fake):
            out = p.invoke_structured_sync("sys", "u" * 20_000, CoderOutput, "smart")

        assert len(calls) == 2, "retry should fire exactly once"
        assert len(out.summary) >= _MIN_CODER_SUMMARY_CHARS
        assert "dropped" in calls[1], "retry should use shrunk context"

    def test_no_retry_when_summary_is_fine(self):
        p = _provider()
        calls = []

        def fake(deployment, tool_name, schema, system_prompt, user_prompt, max_tokens):
            calls.append(user_prompt)
            return _coder_payload("A nice long summary that is clearly fine and complete.")

        with patch.object(p, "_call_chat", side_effect=fake):
            p.invoke_structured_sync("sys", "u", CoderOutput, "smart")

        assert len(calls) == 1, "no retry expected for a good summary"

    def test_retry_failure_keeps_first_result(self):
        p = _provider()
        calls = []

        def fake(deployment, tool_name, schema, system_prompt, user_prompt, max_tokens):
            calls.append(user_prompt)
            if len(calls) == 1:
                return _coder_payload("fix")
            raise RuntimeError("azure blew up on retry")

        with patch.object(p, "_call_chat", side_effect=fake):
            out = p.invoke_structured_sync("sys", "u" * 20_000, CoderOutput, "smart")

        assert len(calls) == 2
        assert out.summary == "fix", "first result kept when retry raises"


class TestValidationRetry:
    def test_validation_error_triggers_one_retry(self):
        p = _provider()
        calls = []

        def fake(deployment, tool_name, schema, system_prompt, user_prompt, max_tokens):
            calls.append(user_prompt)
            if len(calls) == 1:
                # 'files' as a JSON string -> Pydantic validation error
                return {"files": "not-a-list", "summary": "x" * 40, "skipped": []}
            return _coder_payload("A good detailed summary after the retry fixup.")

        with patch.object(p, "_call_chat", side_effect=fake):
            out = p.invoke_structured_sync("sys", "u", CoderOutput, "smart")

        assert len(calls) == 2, "validation failure should retry once"
        assert "RETRY NOTICE" in calls[1]
        assert out.files[0].path == "a.py"


class TestInvokeWithLintFeedback:
    def test_issues_are_appended_to_prompt(self):
        p = _provider()
        calls = []

        def fake(deployment, tool_name, schema, system_prompt, user_prompt, max_tokens):
            calls.append(user_prompt)
            return _coder_payload("Corrected the hallucinated import as instructed.")

        with patch.object(p, "_call_chat", side_effect=fake):
            p.invoke_with_lint_feedback(
                "sys", "original prompt", CoderOutput,
                ["hallucinated import app.fake"], "smart",
            )

        assert "LINT REJECTION" in calls[0]
        assert "app.fake" in calls[0]
