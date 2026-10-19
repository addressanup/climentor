"""Provider interface shared by the mock, Anthropic, and OpenAI providers."""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class TurnMeta:
    """Structured facts about this turn.

    Real providers ignore this (they get the rendered prompt). The mock
    provider builds its deterministic reply from it.
    """

    trigger: str
    learner_text: str = ""
    mode: str = "guided"
    exercise_id: str | None = None
    exercise_title: str | None = None
    stage: str | None = None
    explanation: str | None = None
    prediction_question: str | None = None
    instructions: str | None = None
    hint_text: str | None = None
    hint_level: int = 0
    last_command: str | None = None
    last_exit: int | None = None
    error_excerpt: str | None = None
    verification_summary: str | None = None
    observation_included: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderRequest:
    model: str
    system: str
    messages: list[ChatMessage]
    max_output_tokens: int
    tools: list[ToolSpec] = field(default_factory=list)
    meta: TurnMeta | None = None


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str = ""


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    estimated: bool


@dataclass(frozen=True)
class Done:
    stop_reason: str = ""


StreamEvent = TextDelta | ToolCall | Usage | Done

ErrorKind = Literal[
    "auth",
    "rate_limit",
    "timeout",
    "unavailable",
    "bad_request",
    "not_configured",
    "unknown",
]


class ProviderError(Exception):
    """A provider failure with a learner-safe message (never contains the key)."""

    def __init__(self, kind: ErrorKind, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retry_after = retry_after

    def user_message(self) -> str:
        hints = {
            "auth": "Check the API key with `mentor init` (it is read from Keychain or env).",
            "rate_limit": "The provider asked us to slow down.",
            "timeout": "The provider took too long to answer.",
            "unavailable": "Could not reach the provider (offline or service problem).",
            "bad_request": "The provider rejected the request (often a wrong model id).",
            "not_configured": "Run `mentor init` to choose a provider, key, and model.",
            "unknown": "Unexpected provider error.",
        }
        text = f"{self.message} {hints[self.kind]}"
        if self.retry_after:
            text += f" Try again in about {self.retry_after:.0f}s."
        return text


class Provider(Protocol):
    name: str
    model: str
    makes_network_calls: bool

    def stream(self, request: ProviderRequest) -> AsyncIterator[StreamEvent]: ...

    async def list_models(self) -> list[str]: ...

    async def aclose(self) -> None: ...


def estimate_tokens(text: str) -> int:
    """Rough token estimate (about 4 characters per token). Labelled as an estimate."""
    return math.ceil(len(text) / 4)


def estimate_request_tokens(request: ProviderRequest) -> int:
    total = estimate_tokens(request.system)
    for message in request.messages:
        total += estimate_tokens(message.content) + 4
    return total


def merge_roles(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Merge consecutive same-role messages; ensure the first message is from the user."""
    merged: list[ChatMessage] = []
    for message in messages:
        if merged and merged[-1].role == message.role:
            merged[-1] = ChatMessage(message.role, merged[-1].content + "\n\n" + message.content)
        else:
            merged.append(message)
    if merged and merged[0].role != "user":
        merged.insert(0, ChatMessage("user", "(conversation continues)"))
    return merged
