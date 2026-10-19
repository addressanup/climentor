"""Deterministic offline tutor for tests and demos.

It does not understand free text. It assembles replies from structured turn
facts (lesson text, the actual failing command and error lines). The UI labels
it "MOCK TUTOR" so nobody mistakes it for a real model.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    # Recorded planning answers (tests and offline demos). Without them the mock cannot plan.
    plan: dict[str, Any] | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    assessment: dict[str, Any] | None = None

    @classmethod
    def from_fixture(cls, path: str | Path) -> MockBehaviour:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(plan=data.get("plan"), steps=list(data.get("steps", [])), assessment=data.get("assessment"))


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
            "I can still guide you through your plan.)"
        )
        if meta.extra.get("phase") == "ask_goal":
            parts.append("Tell me what you would like to learn and I will propose a plan.")
        elif meta.exercise_title and meta.stage != "verified":
            step = {
                "explain": "answer the prediction question above, or type `/lesson skip`",
                "attempt": "do the task in the shell or Neovim, then type `/check` (or `/hint` for help)",
            }.get(meta.stage or "", "type `/lesson` to continue")
            parts.append(f"You are on **{meta.exercise_title}**. Next: {step}.")
        elif meta.extra.get("next_step"):
            parts.append(
                f"In **{meta.extra.get('plan_title')}**, next is {meta.extra['next_step']}. Type `/lesson` to start it."
            )
        if meta.last_command is not None:
            parts.append(f"Your last command was `{meta.last_command}` (exit {meta.last_exit}).")
        parts.append("Useful commands: `/lesson`, `/plan`, `/hint`, `/explain <term>`, `/check`, `/help`.")
    return "\n\n".join(parts)


class MockProvider:
    name = "mock"
    makes_network_calls = False

    @property
    def can_plan(self) -> bool:
        return self.behaviour.plan is not None

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
        planned = self._planning(request.meta)
        if planned is not None:
            text, call = planned
            yield TextDelta(text)
            if call is not None:
                yield call
            yield Usage(estimate_request_tokens(request), estimate_tokens(text), estimated=True)
            yield Done("tool_use")
            return
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

    def _planning(self, meta: TurnMeta | None) -> tuple[str, ToolCall | None] | None:
        b = self.behaviour
        trigger = meta.trigger if meta else ""
        if trigger in ("plan", "plan_revise") and b.plan is not None:
            return "Here is a plan for that (recorded mock answer).", ToolCall("propose_plan", b.plan)
        if trigger == "prepare_step" and b.steps:
            index = max(0, int((meta.extra if meta else {}).get("step_position", 1)) - 1) % len(b.steps)
            return "", ToolCall("prepare_step", b.steps[index])
        if trigger == "review_step" and b.assessment is not None:
            return str(b.assessment.get("feedback", "Reviewed.")), ToolCall("assess_step", b.assessment)
        return None

    async def list_models(self) -> list[str]:
        return [self.model]

    async def aclose(self) -> None:
        return None
