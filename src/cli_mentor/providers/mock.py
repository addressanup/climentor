"""Deterministic offline tutor for tests and demos.

It does not understand free text. It assembles replies from structured turn
facts (lesson text, the actual failing command and error lines). The UI labels
it "MOCK TUTOR" so nobody mistakes it for a real model.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from .base import (
    Done,
    ProviderError,
    ProviderRequest,
    StreamEvent,
    TextDelta,
    ToolCall,
    TurnMeta,
    Usage,
    estimate_request_tokens,
    estimate_tokens,
)

LABEL = "MOCK TUTOR (offline, deterministic, not an AI)"


@dataclass
class MockBehaviour:
    chunk_delay: float = 0.0
    fail_with: ProviderError | None = None
    fail_after_chunks: int | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


def compose(meta: TurnMeta | None) -> str:
    if meta is None:
        return "I have no structured information for this turn."
    t = meta.trigger
    parts: list[str] = []
    if t == "lesson_intro":
        parts.append(f"## {meta.exercise_title or 'New exercise'}")
        if meta.explanation:
            parts.append(meta.explanation)
        if meta.prediction_question:
            parts.append(f"**Predict first:** {meta.prediction_question}")
        if meta.instructions:
            parts.append(f"**Your task:**\n\n{meta.instructions}")
        parts.append("When you think you are done, type `/check`. Ask for `/hint` any time.")
    elif t in ("command_failed", "repeated_failure"):
        if meta.last_command is not None:
            parts.append(f"I saw `{meta.last_command}` finish with exit status {meta.last_exit}.")
        if meta.error_excerpt:
            parts.append(f"The terminal showed:\n\n```\n{meta.error_excerpt}\n```")
        if meta.hint_text:
            parts.append(f"**Small hint ({meta.hint_level}):** {meta.hint_text}")
        else:
            parts.append("Read the error message word by word. What is it telling you?")
    elif t == "hint":
        if meta.hint_text:
            parts.append(f"**Hint {meta.hint_level}:** {meta.hint_text}")
        else:
            parts.append("There are no more hints for this exercise. Try `/explain`.")
    elif t == "explain":
        if meta.explanation:
            parts.append(meta.explanation)
        else:
            parts.append(
                f"I can only explain terms from the current lesson offline. You asked about: `{meta.learner_text}`."
            )
    elif t == "verification":
        parts.append(meta.verification_summary or "The checker returned no summary.")
        if meta.extra.get("passed"):
            parts.append("Now explain in one sentence *why* your command worked.")
        else:
            parts.append("Look at the failing check above, change one thing, and run `/check` again.")
    elif t == "session_start":
        parts.append(meta.learner_text or "Welcome back.")
    elif t == "prediction":
        parts.append(f"You predicted: “{meta.learner_text}”.")
        expected = meta.extra.get("expected_prediction")
        if expected:
            parts.append(f"The expected answer is: **{expected}**. If yours differs, that is useful — notice why.")
        if meta.instructions:
            parts.append(f"**Your task:**\n\n{meta.instructions}")
        parts.append("When you think you are done, type `/check`. Ask for `/hint` any time.")
    elif t == "review":
        parts.append(f"Review time. {meta.learner_text}")
        parts.append("Explain in your own words what each skill does, then try a small example in the shell.")
    else:
        parts.append(
            "Hi, I am your CLI Mentor tutor. (This is the offline mock, so I cannot understand free text; "
            "I can still guide you through the lessons.)"
        )
        next_title = meta.extra.get("next_lesson_title")
        if meta.exercise_title and meta.stage != "verified":
            step = {
                "explain": "answer the prediction question above, or type `/lesson skip`",
                "attempt": "do the task in the shell or Neovim, then type `/check` (or `/hint` for help)",
            }.get(meta.stage or "", "type `/lesson` to continue")
            parts.append(f"You are on **{meta.exercise_title}**. Next: {step}.")
        elif next_title:
            intro = "Your first exercise" if not meta.extra.get("completed_count") else "Next up"
            parts.append(f"{intro}: **{next_title}**. Type `/lesson` to start it.")
        if meta.last_command is not None:
            parts.append(f"Your last command was `{meta.last_command}` (exit {meta.last_exit}).")
        parts.append("Useful commands: `/lesson`, `/hint`, `/explain <term>`, `/check`, `/progress`, `/help`.")
    return "\n\n".join(parts)


class MockProvider:
    name = "mock"
    makes_network_calls = False

    def __init__(self, model: str = "mock-tutor", behaviour: MockBehaviour | None = None) -> None:
        self.model = model
        self.behaviour = behaviour or MockBehaviour()
        self.requests: list[ProviderRequest] = []
        self.cancelled = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        b = self.behaviour
        if b.fail_with is not None and b.fail_after_chunks is None:
            raise b.fail_with
        text = compose(request.meta)
        words = text.split(" ")
        try:
            for i, word in enumerate(words):
                if b.fail_with is not None and b.fail_after_chunks is not None and i >= b.fail_after_chunks:
                    raise b.fail_with
                if b.chunk_delay:
                    await asyncio.sleep(b.chunk_delay)
                yield TextDelta(word if i == len(words) - 1 else word + " ")
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        for call in b.tool_calls:
            yield call
        yield Usage(estimate_request_tokens(request), estimate_tokens(text), estimated=True)
        yield Done("end_turn")

    async def list_models(self) -> list[str]:
        return [self.model]

    async def aclose(self) -> None:
        return None
