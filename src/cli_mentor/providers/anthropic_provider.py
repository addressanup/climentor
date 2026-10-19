"""Anthropic provider using the official ``anthropic`` SDK (Messages streaming)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anthropic
import httpx2

from ..config import ProviderSettings
from .base import (
    Done,
    ProviderError,
    ProviderRequest,
    StreamEvent,
    TextDelta,
    ToolCall,
    Usage,
    merge_roles,
)


def _retry_after(exc: anthropic.APIStatusError) -> float | None:
    value = exc.response.headers.get("retry-after") if exc.response is not None else None
    try:
        return float(value) if value else None
    except ValueError:
        return None


def map_error(exc: BaseException) -> ProviderError:
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return ProviderError("auth", "Anthropic rejected the API key or permissions.")
    if isinstance(exc, anthropic.RateLimitError):
        return ProviderError("rate_limit", "Anthropic rate limit reached.", _retry_after(exc))
    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderError("timeout", "Anthropic request timed out.")
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderError("unavailable", "Could not connect to Anthropic.")
    if isinstance(exc, anthropic.BadRequestError | anthropic.NotFoundError):
        return ProviderError("bad_request", f"Anthropic rejected the request ({type(exc).__name__}).")
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500:
            return ProviderError("unavailable", f"Anthropic service error (HTTP {exc.status_code}).")
        return ProviderError("unknown", f"Anthropic error (HTTP {exc.status_code}).")
    return ProviderError("unknown", f"Anthropic error ({type(exc).__name__}).")


class AnthropicProvider:
    name = "anthropic"
    makes_network_calls = True

    def __init__(
        self,
        api_key: str,
        settings: ProviderSettings,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        if not settings.model:
            raise ProviderError("not_configured", "No Anthropic model is configured.")
        self.model = settings.model
        self.settings = settings
        self.client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=settings.base_url or None,
            timeout=settings.timeout_seconds,
            max_retries=settings.max_retries,
            http_client=http_client,
        )

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamEvent]:
        kwargs: dict[str, Any] = {
            "model": request.model or self.model,
            "max_tokens": request.max_output_tokens,
            "system": request.system,
            "messages": [{"role": m.role, "content": m.content} for m in merge_roles(request.messages)],
        }
        if request.tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in request.tools
            ]
        try:
            async with self.client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    if event.type == "text":
                        yield TextDelta(event.text)
                final = await stream.get_final_message()
        except anthropic.AnthropicError as exc:
            raise map_error(exc) from None
        for block in final.content:
            if block.type == "tool_use":
                args = block.input if isinstance(block.input, dict) else {}
                yield ToolCall(block.name, dict(args), block.id)
        yield Usage(final.usage.input_tokens, final.usage.output_tokens, estimated=False)
        yield Done(str(final.stop_reason or ""))

    async def list_models(self) -> list[str]:
        try:
            page = await self.client.models.list(limit=100)
        except anthropic.AnthropicError as exc:
            raise map_error(exc) from None
        return [m.id for m in page.data]

    async def aclose(self) -> None:
        await self.client.close()
