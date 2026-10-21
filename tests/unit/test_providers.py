"""Provider tests.

The Anthropic and OpenAI tests run the *real* SDK code paths (request
building, SSE parsing, retries, error mapping) against recorded responses via
``httpx2.MockTransport``. No network, no API key.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from cli_mentor.config import Config, ProviderSettings
from cli_mentor.providers import build_provider
from cli_mentor.providers.anthropic_provider import AnthropicProvider
from cli_mentor.providers.base import (
    ChatMessage,
    Done,
    ProviderError,
    ProviderRequest,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolSpec,
    TurnMeta,
    Usage,
    merge_roles,
)
from cli_mentor.providers.mock import MockBehaviour, MockProvider
from cli_mentor.providers.openai_provider import THINKING_ROOM, OpenAIProvider

FAKE_KEY = "sk-test-FAKEKEY-not-real-0123456789"
TOOL = ToolSpec(
    "propose_memory",
    "Propose a memory",
    {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
)


def request(**kw: Any) -> ProviderRequest:
    base: dict[str, Any] = dict(
        model="",
        system="You are a tutor.",
        messages=[ChatMessage("user", "hi")],
        max_output_tokens=200,
        tools=[TOOL],
    )
    base.update(kw)
    return ProviderRequest(**base)


async def collect(provider: Any, req: ProviderRequest) -> list[StreamEvent]:
    return [e async for e in provider.stream(req)]


def sse(events: list[tuple[str | None, dict[str, Any] | str]]) -> bytes:
    out = []
    for name, data in events:
        if name:
            out.append(f"event: {name}")
        out.append("data: " + (data if isinstance(data, str) else json.dumps(data)))
        out.append("")
    return ("\n".join(out) + "\n").encode()


def transport(handler: Callable[[httpx2.Request], httpx2.Response]) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


ANTHROPIC_STREAM = sse(
    [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "test-model",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 12, "output_tokens": 1},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hello "},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "learner"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "propose_memory",
                    "input": {},
                },
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"text": "likes pipes"}'},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": 9},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
)


async def test_anthropic_stream_text_tool_usage() -> None:
    seen: list[httpx2.Request] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.append(req)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=ANTHROPIC_STREAM)

    p = AnthropicProvider(
        FAKE_KEY,
        ProviderSettings(model="test-model", max_retries=0),
        http_client=transport(handler),
    )
    events = await collect(p, request(messages=[ChatMessage("assistant", "earlier"), ChatMessage("user", "hi")]))
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hello ", "learner"]
    assert ToolCall("propose_memory", {"text": "likes pipes"}, "toolu_1") in events
    usage = next(e for e in events if isinstance(e, Usage))
    assert usage == Usage(12, 9, estimated=False)
    assert isinstance(events[-1], Done) and events[-1].stop_reason == "tool_use"
    body = json.loads(seen[0].content)
    assert body["model"] == "test-model"
    assert body["system"] == "You are a tutor."
    assert body["messages"][0]["role"] == "user"  # first turn forced to user
    assert body["tools"][0]["input_schema"]["required"] == ["text"]
    assert FAKE_KEY not in json.dumps(body)


@pytest.mark.parametrize(
    "status,kind",
    [(401, "auth"), (429, "rate_limit"), (400, "bad_request"), (503, "unavailable")],
)
async def test_anthropic_error_mapping(status: int, kind: str) -> None:
    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            status,
            headers={"retry-after": "7"},
            json={"type": "error", "error": {"type": "x", "message": f"boom {FAKE_KEY}"}},
        )

    p = AnthropicProvider(FAKE_KEY, ProviderSettings(model="m", max_retries=0), http_client=transport(handler))
    with pytest.raises(ProviderError) as info:
        await collect(p, request())
    assert info.value.kind == kind
    assert FAKE_KEY not in str(info.value) and FAKE_KEY not in info.value.user_message()
    if kind == "rate_limit":
        assert info.value.retry_after == 7


async def test_anthropic_bounded_retries_then_success() -> None:
    calls = 0

    def handler(req: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx2.Response(
                500,
                headers={"retry-after": "0"},
                json={"type": "error", "error": {"type": "api_error", "message": "x"}},
            )
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=ANTHROPIC_STREAM)

    p = AnthropicProvider(FAKE_KEY, ProviderSettings(model="m", max_retries=1), http_client=transport(handler))
    events = await collect(p, request())
    assert calls == 2 and any(isinstance(e, Done) for e in events)


async def test_anthropic_connection_and_timeout_errors() -> None:
    def refuse(req: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=req)

    p = AnthropicProvider(FAKE_KEY, ProviderSettings(model="m", max_retries=0), http_client=transport(refuse))
    with pytest.raises(ProviderError) as info:
        await collect(p, request())
    assert info.value.kind == "unavailable"

    def slow(req: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=req)

    p = AnthropicProvider(FAKE_KEY, ProviderSettings(model="m", max_retries=0), http_client=transport(slow))
    with pytest.raises(ProviderError) as info:
        await collect(p, request())
    assert info.value.kind == "timeout"


def openai_chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


OPENAI_STREAM = sse(
    [
        (None, openai_chunk({"role": "assistant", "content": "Hel"})),
        (None, openai_chunk({"content": "lo"})),
        (
            None,
            openai_chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "propose_memory", "arguments": '{"te'},
                        }
                    ]
                }
            ),
        ),
        (
            None,
            openai_chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'xt": "x"}'}}]}, "tool_calls"),
        ),
        (
            None,
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "m",
                "choices": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        ),
        (None, "[DONE]"),
    ]
)


async def test_openai_stream_text_tool_usage() -> None:
    seen: list[httpx2.Request] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.append(req)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=OPENAI_STREAM)

    p = OpenAIProvider(FAKE_KEY, ProviderSettings(model="m", max_retries=0), http_client=transport(handler))
    events = await collect(p, request())
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hello"
    assert ToolCall("propose_memory", {"text": "x"}, "call_1") in events
    assert Usage(10, 5, False) in events
    body = json.loads(seen[0].content)
    assert body["messages"][0] == {"role": "system", "content": "You are a tutor."}
    assert body["stream_options"] == {"include_usage": True}
    assert "max_completion_tokens" in body and "max_tokens" not in body


async def test_openai_compatible_base_url_uses_max_tokens() -> None:
    seen: list[httpx2.Request] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.append(req)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=OPENAI_STREAM)

    p = OpenAIProvider(
        FAKE_KEY,
        ProviderSettings(model="m", max_retries=0, base_url="http://localhost:9/v1"),
        http_client=transport(handler),
    )
    await collect(p, request())
    assert str(seen[0].url).startswith("http://localhost:9/v1")
    assert "max_tokens" in json.loads(seen[0].content)


async def test_thinking_models_get_room_on_top_of_the_reply() -> None:
    """DeepSeek's hidden thinking used the reply's own limit and cut replies off mid-sentence."""
    thinking = sse(
        [
            (None, openai_chunk({"role": "assistant", "content": None, "reasoning_content": "Let me think"})),
            (None, openai_chunk({"content": "Answer"}, "stop")),
            (None, "[DONE]"),
        ]
    )
    seen: list[httpx2.Request] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.append(req)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=thinking)

    def provider(base_url: str) -> OpenAIProvider:
        settings = ProviderSettings(model="m", max_retries=0, base_url=base_url)
        return OpenAIProvider(FAKE_KEY, settings, http_client=transport(handler))

    def limit() -> int:
        body = json.loads(seen[-1].content)
        return int(body.get("max_tokens") or body["max_completion_tokens"])

    for known in ("https://api.deepseek.com", ""):  # DeepSeek, and OpenAI itself
        await collect(provider(known), request())
        assert limit() == 200 + THINKING_ROOM
    other = provider("http://localhost:9/v1")
    events = await collect(other, request())
    assert limit() == 200  # a server not known to think gets exactly what was asked for...
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Answer"  # (thinking is not shown)
    await collect(other, request())
    assert limit() == 200 + THINKING_ROOM  # ...until one of its replies shows that it thinks


@pytest.mark.parametrize(
    "status,kind", [(401, "auth"), (429, "rate_limit"), (404, "bad_request"), (500, "unavailable")]
)
async def test_openai_error_mapping(status: int, kind: str) -> None:
    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, headers={"retry-after": "3"}, json={"error": {"message": f"bad {FAKE_KEY}"}})

    p = OpenAIProvider(FAKE_KEY, ProviderSettings(model="m", max_retries=0), http_client=transport(handler))
    with pytest.raises(ProviderError) as info:
        await collect(p, request())
    assert info.value.kind == kind
    assert FAKE_KEY not in info.value.user_message()


async def test_openai_missing_usage_is_estimated() -> None:
    stream = sse([(None, openai_chunk({"content": "abc"}, "stop")), (None, "[DONE]")])

    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=stream)

    p = OpenAIProvider(FAKE_KEY, ProviderSettings(model="m", max_retries=0), http_client=transport(handler))
    usage = next(e for e in await collect(p, request()) if isinstance(e, Usage))
    assert usage.estimated


# ---------- mock ----------
async def test_mock_uses_actual_error_and_hint() -> None:
    p = MockProvider()
    meta = TurnMeta(
        trigger="command_failed",
        last_command="grep ERROR sever.log",
        last_exit=2,
        error_excerpt="grep: sever.log: No such file or directory",
        hint_text="Check the file name.",
        hint_level=1,
    )
    events = await collect(p, request(meta=meta))
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert "sever.log: No such file" in text and "Check the file name." in text
    assert next(e for e in events if isinstance(e, Usage)).estimated
    assert p.requests[0].meta is meta


async def test_mock_failure_and_cancellation() -> None:
    p = MockProvider(behaviour=MockBehaviour(fail_with=ProviderError("unavailable", "down")))
    with pytest.raises(ProviderError):
        await collect(p, request(meta=TurnMeta(trigger="hint")))
    slow = MockProvider(behaviour=MockBehaviour(chunk_delay=0.05))
    task = asyncio.create_task(collect(slow, request(meta=TurnMeta(trigger="lesson_intro", explanation="word " * 50))))
    await asyncio.sleep(0.12)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slow.cancelled == 1


def test_build_provider_requires_key_and_model() -> None:
    cfg = Config()
    assert isinstance(build_provider(cfg), MockProvider)
    cfg.tutor.provider = "anthropic"
    with pytest.raises(ProviderError) as info:
        build_provider(cfg)
    assert info.value.kind == "not_configured"


def test_build_provider_with_keychain_key(memory_keyring: Any) -> None:
    memory_keyring.set_password("cli-mentor", "openai", FAKE_KEY)
    cfg = Config()
    cfg.tutor.provider = "openai"
    with pytest.raises(ProviderError):  # model not set yet
        build_provider(cfg)
    cfg.providers.openai.model = "some-model"
    assert isinstance(build_provider(cfg), OpenAIProvider)


def test_merge_roles() -> None:
    merged = merge_roles([ChatMessage("assistant", "a"), ChatMessage("user", "b"), ChatMessage("user", "c")])
    assert [m.role for m in merged] == ["user", "assistant", "user"]
    assert merged[-1].content == "b\n\nc"
