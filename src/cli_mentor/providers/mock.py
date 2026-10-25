"""Deterministic offline tutor for tests and demos.

It does not understand free text. It assembles replies from structured turn
facts (lesson text, the actual failing command and error lines). The UI labels
it "MOCK TUTOR" so nobody mistakes it for a real model.
"""

from __future__ import annotations

import asyncio
import json
import re
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
DONE_LINE = "When you think you are done, just tell me (“done”). Say “hint” any time you are stuck."


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
    reply: dict[str, Any] | None = None  # recorded interpretation; the mock cannot infer meaning

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
        parts.append(DONE_LINE)
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
            parts.append("There are no more hints for this exercise. Ask me to explain a word you are unsure about.")
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
            parts.append(str(meta.extra.get("open_question") or "Why did that work?"))
        else:
            parts.append("What is one small change you could try?")
    elif t == "session_start":
        parts.append(meta.learner_text or "Welcome back.")
    elif t == "prediction":
        parts.append(f"You predicted: “{meta.learner_text}”.")
        expected = meta.extra.get("expected_prediction")
        if expected:
            parts.append(f"The expected answer is: **{expected}**. If yours differs, that is useful — notice why.")
        if meta.instructions:
            parts.append(f"**Your task:**\n\n{meta.instructions}")
        parts.append(DONE_LINE)
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
                "explain": "answer the prediction question above in your own words, or say “skip”",
                "attempt": "do the task in the shell or Neovim, then say “done” (or “hint” if you are stuck)",
            }.get(meta.stage or "", "say “next” to continue")
            parts.append(f"You are on **{meta.exercise_title}**. Next: {step}.")
        elif meta.extra.get("next_step"):
            parts.append(
                f"In **{meta.extra.get('plan_title')}**, next is {meta.extra['next_step']}. Say “next” to start it."
            )
        if meta.last_command is not None:
            parts.append(f"Your last command was `{meta.last_command}` (exit {meta.last_exit}).")
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
        planned = self._planning(request)
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

    def _planning(self, request: ProviderRequest) -> tuple[str, ToolCall | None] | None:
        b = self.behaviour
        meta = request.meta
        trigger = meta.trigger if meta else ""
        if meta is not None and any(t.name == "interpret_reply" for t in request.tools or []):
            # Deterministic fixture behaviour, not semantic reasoning. Tests can record an interpretation.
            from ..curriculum.adapters.generic import answer_matches

            text = meta.learner_text.strip()
            checks = meta.extra.get("answer_checks", [])
            clarification = bool(re.match(r"^(before\b|explain\b|what\b|why\b|how\b|can you\b)", text, re.I))
            verdict = "understood"
            if not re.search(r"\w", text):
                verdict = "no_answer"
            elif clarification:
                verdict = "clarification"
            elif trigger != "prediction" and meta.stage != "reflect" and checks:
                verdict = "understood" if all(answer_matches(text, c["accepted"]) for c in checks) else "needs_work"
            reply = b.reply or {
                "verdict": verdict,
                "feedback": "That fits the example."
                if verdict == "understood"
                else "Let's look at that part together.",
                "follow_up": "Which part of the example helps you decide?" if verdict == "needs_work" else "",
                "independent": False,
            }
            return "", ToolCall("interpret_reply", reply)
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
