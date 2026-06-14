"""
LLM provider factory — the single switch between Azure OpenAI and Bedrock.

ShipMate's agents (coder, decomposer) and LLMService all need a structured-
output LLM client. There are two interchangeable implementations:

  • AzureOpenAIProvider  — Azure OpenAI tool-calling. The default, hosted
                           production path (Azure AI Foundry deployment).
  • BedrockProvider      — AWS Bedrock Converse API. Optional alternative
                           backend (standard boto3 credential provider chain).

Both expose the IDENTICAL contract:
    invoke_structured_sync(system_prompt, user_prompt, schema_class, deployment_hint) -> BaseModel
    invoke_structured(...)            (async wrapper)
    invoke_with_lint_feedback(...)    (coder retry path)

Selection is one env var, `SHIPMATE_LLM_PROVIDER`:
    - "azure" (default)  → AzureOpenAIProvider
    - "bedrock"          → BedrockProvider

So the SAME codebase runs on Azure OpenAI by default and can fall back to
Bedrock by setting SHIPMATE_LLM_PROVIDER=bedrock + AWS creds. No code changes
to switch — just the flag.

The provider is built once and cached per process. `reset_provider()` clears
the cache (used on transient auth failures so the next call rebuilds the
client with refreshed credentials without a process restart).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("shipmate.llm_provider")

# Accepted values for the provider switch.
_BEDROCK = "bedrock"
_AZURE = "azure"

_provider: Optional[Any] = None
_provider_kind: Optional[str] = None


def provider_kind() -> str:
    """The configured provider name (lowercased), defaulting to azure.

    SHIPMATE_LLM_PROVIDER is the canonical key. We also honour the older
    LLM_PROVIDER key (llm_service used it) so existing .env files keep working;
    SHIPMATE_LLM_PROVIDER wins if both are set.
    """
    kind = (
        os.getenv("SHIPMATE_LLM_PROVIDER")
        or os.getenv("LLM_PROVIDER")
        or _AZURE
    ).strip().lower()
    return kind if kind in (_BEDROCK, _AZURE) else _AZURE


def get_provider() -> Any:
    """Return the configured provider singleton, building it on first use.

    Raises on construction failure (missing SDK / missing Azure config) rather
    than silently returning None — the agents need a real provider, and a clear
    error beats a confusing 'enhancements disabled'. LLMService keeps its own
    None-tolerant wrapper for the optional enhancement path.
    """
    global _provider, _provider_kind
    kind = provider_kind()
    if _provider is not None and _provider_kind == kind:
        return _provider

    if kind == _AZURE:
        from app.services.azure_openai_provider import AzureOpenAIProvider
        _provider = AzureOpenAIProvider()
    else:
        from app.services.bedrock_provider import BedrockProvider
        _provider = BedrockProvider()

    _provider_kind = kind
    logger.info("LLM provider ready: %s", kind)
    return _provider


def reset_provider() -> None:
    """Drop the cached provider so the next get_provider() rebuilds it.
    Used after a transient auth failure (e.g. Bedrock ADA creds rolled over)
    so the fresh client picks up new credentials without a process restart."""
    global _provider, _provider_kind
    _provider = None
    _provider_kind = None
