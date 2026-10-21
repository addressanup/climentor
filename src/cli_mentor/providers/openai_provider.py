"""OpenAI provider using the official ``openai`` SDK (Chat Completions streaming).

Chat Completions is used because OpenAI-compatible servers (set ``base_url``)
support it widely. Those servers are untested unless noted in STATUS.md.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import httpx2
import openai

from ..config import ProviderSettings
from .base import (
    Done,
    ProviderError,
    ProviderRequest,
    StreamEvent,
    TextDelta,
    ToolCall,
    Usage,
    estimate_request_tokens,
    estimate_tokens,
)

log = logging.getLogger(__name__)

# Models that think before answering (DeepSeek, OpenAI's reasoning models) spend the same output limit on
# hidden thinking, which cut replies off mid-sentence. This is extra room on top of the reply's own limit;
# it is a ceiling, so it costs nothing unless the model uses it.
THINKING_ROOM = 8000


def _retry_after(exc: openai.APIStatusError) -> float | None:
    value = exc.response.headers.get("retry-after") if exc.response is not None else None
    try:
        return float(value) if value else None
    except ValueError:
        return None


def map_error(exc: BaseException, who: str = "OpenAI") -> ProviderError:
    """Map SDK errors to learner-safe messages. ``who`` names the endpoint that answered."""
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return ProviderError("auth", f"{who} rejected the API key or permissions.")
    if isinstance(exc, openai.RateLimitError):
        return ProviderError("rate_limit", f"{who}: rate limit reached.", _retry_after(exc))
    if isinstance(exc, openai.APITimeoutError):
        return ProviderError("timeout", f"{who}: request timed out.")
    if isinstance(exc, openai.APIConnectionError):
        return ProviderError("unavailable", f"Could not connect to {who}.")
    if isinstance(exc, openai.BadRequestError | openai.NotFoundError):
        return ProviderError("bad_request", f"{who} rejected the request ({type(exc).__name__}).")
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code >= 500:
            return ProviderError("unavailable", f"{who}: service error (HTTP {exc.status_code}).")
        return ProviderError("unknown", f"{who}: error (HTTP {exc.status_code}).")
    return ProviderError("unknown", f"{who}: error ({type(exc).__name__}).")


class OpenAIProvider:
    name = "openai"
    makes_network_calls = True

    def __init__(
        self,
        api_key: str,
        settings: ProviderSettings,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        if not settings.model:
            raise ProviderError("not_configured", "No OpenAI model is configured.")
        self.model = settings.model
        self.settings = settings
        # Name the endpoint in errors, so a key sent to the wrong service is obvious.
        self.endpoint = f"The server at {settings.base_url}" if settings.base_url else "OpenAI (api.openai.com)"
        # Known to think by default; any other server is treated as thinking once a reply shows it does.
        host = urlparse(settings.base_url).hostname or ""
        self.thinks = not settings.base_url or host == "deepseek.com" or host.endswith(".deepseek.com")
        self.client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=settings.base_url or None,
            timeout=settings.timeout_seconds,
            max_retries=settings.max_retries,
            http_client=http_client,
        )

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamEvent]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": request.system}]
        messages += [{"role": m.role, "content": m.content} for m in request.messages]
        kwargs: dict[str, Any] = {
            "model": request.model or self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        # Official OpenAI uses max_completion_tokens; many compatible servers
        # only understand max_tokens.
        limit = request.max_output_tokens + (THINKING_ROOM if self.thinks else 0)
        kwargs["max_tokens" if self.settings.base_url else "max_completion_tokens"] = limit
        if request.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in request.tools
            ]
        text_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        usage: Usage | None = None
        stop = ""
        try:
            stream = await self.client.chat.completions.create(**kwargs)
            try:
                async for chunk in stream:
                    if chunk.usage is not None:
                        usage = Usage(chunk.usage.prompt_tokens, chunk.usage.completion_tokens, False)
                        details = chunk.usage.completion_tokens_details
                        if details is not None and details.reasoning_tokens:
                            self.thinks = True
                    for choice in chunk.choices:
                        delta = choice.delta
                        if getattr(delta, "reasoning_content", None):  # DeepSeek streams its thinking here
                            self.thinks = True
                        if delta.content:
                            text_parts.append(delta.content)
                            yield TextDelta(delta.content)
                        for tc in delta.tool_calls or []:
                            slot = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                            if tc.id:
                                slot["id"] = tc.id
                            if tc.function is not None:
                                slot["name"] += tc.function.name or ""
                                slot["args"] += tc.function.arguments or ""
                        if choice.finish_reason:
                            stop = choice.finish_reason
            finally:
                await stream.close()
        except openai.OpenAIError as exc:
            raise map_error(exc, self.endpoint) from None
        for slot in calls.values():
            try:
                args = json.loads(slot["args"] or "{}")
            except json.JSONDecodeError:
                log.warning("ignoring tool call with invalid JSON arguments")
                continue
            if isinstance(args, dict) and slot["name"]:
                yield ToolCall(slot["name"], args, slot["id"])
        if usage is None:
            usage = Usage(estimate_request_tokens(request), estimate_tokens("".join(text_parts)), True)
        yield usage
        yield Done(stop)

    async def list_models(self) -> list[str]:
        try:
            page = await self.client.models.list()
        except openai.OpenAIError as exc:
            raise map_error(exc, self.endpoint) from None
        return sorted(m.id for m in page.data)

    async def aclose(self) -> None:
        await self.client.close()
