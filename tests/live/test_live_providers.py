"""Optional tests against real providers. They cost a few tokens.

Run with:  uv run pytest -m live
Needs, per provider (environment variables only; tests never read the real
Keychain):
  CLI_MENTOR_LIVE_ANTHROPIC_KEY and CLI_MENTOR_LIVE_ANTHROPIC_MODEL
  CLI_MENTOR_LIVE_OPENAI_KEY    and CLI_MENTOR_LIVE_OPENAI_MODEL
(The usual ANTHROPIC_API_KEY / OPENAI_API_KEY are removed by tests/conftest.py
so ordinary test runs can never spend money.)
Model ids are never hard-coded; choose one your account can use.
"""

from __future__ import annotations

import os

import pytest

from cli_mentor.config import ProviderSettings
from cli_mentor.providers.base import ChatMessage, Done, ProviderRequest, TextDelta, Usage

pytestmark = pytest.mark.live


def _setup(provider: str) -> tuple[str, str]:
    model = os.environ.get(f"CLI_MENTOR_LIVE_{provider.upper()}_MODEL", "")
    key = os.environ.get(f"CLI_MENTOR_LIVE_{provider.upper()}_KEY", "")
    if not model or not key:
        pytest.skip(f"set a {provider} key and CLI_MENTOR_LIVE_{provider.upper()}_MODEL to run")
    return key, model


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
async def test_live_stream(provider: str) -> None:
    key, model = _setup(provider)
    settings = ProviderSettings(model=model, max_output_tokens=64, max_retries=1, timeout_seconds=60)
    if provider == "anthropic":
        from cli_mentor.providers.anthropic_provider import AnthropicProvider

        client = AnthropicProvider(key, settings)
    else:
        from cli_mentor.providers.openai_provider import OpenAIProvider

        client = OpenAIProvider(key, settings)
    request = ProviderRequest(
        model=model,
        system="Reply with one short sentence.",
        messages=[ChatMessage("user", "Say hello to a new CLI learner.")],
        max_output_tokens=64,
    )
    events = [e async for e in client.stream(request)]
    await client.aclose()
    assert "".join(e.text for e in events if isinstance(e, TextDelta)).strip()
    usage = next(e for e in events if isinstance(e, Usage))
    assert usage.input_tokens > 0
    assert isinstance(events[-1], Done)
