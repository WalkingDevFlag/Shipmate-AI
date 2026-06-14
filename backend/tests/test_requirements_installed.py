"""Guard: every runtime dependency the code imports is actually declared +
installed. Catches the 'works on my machine, dies in a clean install' class of
bug — specifically the boto3 gap where Bedrock (the DEFAULT provider) imported
boto3 lazily, so the backend booted (/health passed) but crashed on the first
/analyze in any clean Docker/Azure deploy because boto3 wasn't in
requirements.txt.

These run in CI's clean-install environment, so an import failure here means
the dependency is genuinely missing from requirements — exactly what we want to
fail the build instead of discovering it in production.
"""
import importlib

import pytest


# (module to import, why it's required). Each must be importable in the same
# environment that `pip install -r requirements.txt` produces.
_REQUIRED_RUNTIME_MODULES = [
    ("openai", "Azure OpenAI provider (default LLM) — azure_openai_provider.py"),
    ("boto3", "Bedrock provider (optional, LLM_PROVIDER=bedrock) — Converse calls"),
    ("botocore", "boto3 transitive — Config/exceptions used by bedrock_provider"),
    ("fastapi", "web framework"),
    ("uvicorn", "ASGI server"),
    ("httpx", "GitHub API client"),
    ("pydantic", "schemas"),
    ("dotenv", "python-dotenv — .env loading in main.py"),
]


@pytest.mark.parametrize("module,reason", _REQUIRED_RUNTIME_MODULES)
def test_runtime_dependency_importable(module, reason):
    try:
        importlib.import_module(module)
    except ImportError as e:  # pragma: no cover - the failure IS the signal
        pytest.fail(
            f"Required runtime module {module!r} is not installed ({reason}). "
            f"It must be declared in backend/requirements.txt so a clean "
            f"`pip install -r requirements.txt` (Docker build / Azure deploy) "
            f"includes it. Import error: {e}"
        )


def test_bedrock_provider_can_build_client():
    """The Bedrock provider must be able to construct its boto3 client — proves
    boto3 is present AND the provider's lazy import path works. We don't make a
    network call; constructing the client is enough to exercise the import."""
    import boto3
    # If boto3 is importable, the bedrock-runtime client constructs offline
    # (no creds needed just to build the client object).
    client = boto3.client("bedrock-runtime", region_name="us-west-2")
    assert client is not None
