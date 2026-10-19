"""Model providers and a factory that builds the configured one."""

from __future__ import annotations

from ..apikeys import get_api_key
from ..config import Config, ProviderName
from ..logs import register_secret
from .base import Provider, ProviderError
from .mock import MockProvider


def build_provider(config: Config, name: ProviderName | None = None) -> Provider:
    """Build a provider. Raises ``ProviderError('not_configured')`` with guidance."""
    chosen = name or config.tutor.provider
    settings = config.provider_settings(chosen)
    if chosen == "mock":
        return MockProvider(settings.model or "mock-tutor")
    lookup = get_api_key(chosen)
    if lookup.key is None:
        raise ProviderError(
            "not_configured",
            f"No API key for {chosen} (looked in the environment and the macOS Keychain).",
        )
    register_secret(lookup.key)
    if chosen == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(lookup.key, settings)
    from .openai_provider import OpenAIProvider

    return OpenAIProvider(lookup.key, settings)


__all__ = ["MockProvider", "Provider", "ProviderError", "build_provider"]
