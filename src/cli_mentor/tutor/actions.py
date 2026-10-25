"""The only actions a model may propose, and their validation.

Proposals are validated with Pydantic. Valid proposals are still subject to
deterministic permission checks in the engine. Invalid ones are dropped.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..providers.base import ToolCall, ToolSpec


class ProposeMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["profile", "skill", "note"] = "note"
    text: str = Field(min_length=3, max_length=300)


class RequestCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="", max_length=200)
    # Legacy shortcut for factual checks; ordinary conversation uses interpret_reply.
    answer: str = Field(default="", max_length=200)


class StartStep(BaseModel):
    """The learner said, in their own words, that they are ready for the next step."""

    model_config = ConfigDict(extra="forbid")


class AssessStep(BaseModel):
    """The tutor's review of a step that has no automatic check (recorded as unverified)."""

    model_config = ConfigDict(extra="forbid")
    verdict: Literal["complete", "needs_work"]
    feedback: str = Field(default="", max_length=600)


class InterpretReply(BaseModel):
    """A semantic reading of the actual learner reply, never an execution check."""

    model_config = ConfigDict(extra="forbid")
    verdict: Literal["understood", "needs_work", "clarification", "no_answer"]
    feedback: str = Field(min_length=1, max_length=600)
    follow_up: str = Field(default="", max_length=400)
    independent: bool = Field(
        default=False,
        description="True only for correct application or transfer without a leading hint or supplied answer. "
        "Repeating an example, guessing, or a successful run alone is not independent understanding.",
    )
    answers_task: bool = Field(
        default=False,
        description="For a prediction on a concept-only step: true if this reply also answers the task's "
        "concept question, so the learner is not asked to repeat the same answer.",
    )


REPLY_TOOL = ToolSpec(
    "interpret_reply",
    "Read the learner's own words for meaning, accepting synonyms and paraphrases. Return understood, "
    "needs_work (correct only the misconception and ask a smaller question), clarification (answer their "
    "question without consuming the pending answer), or no_answer (no usable answer arrived; ask again). "
    "Give brief, friendly feedback, never a score or pass/fail report. Never invent an answer.",
    InterpretReply.model_json_schema(),
)


ASSESS_TOOL = ToolSpec(
    "assess_step",
    "Give your verdict on the learner's work for a step that has no automatic check: 'complete' or "
    "'needs_work', with one or two sentences of feedback. Your verdict is recorded as unverified.",
    AssessStep.model_json_schema(),
)

START_TOOL = ToolSpec(
    "start_step",
    "Continue into the next idea once the current idea is understood, or when the learner asks to move on.",
    StartStep.model_json_schema(),
)

TOOLS: list[ToolSpec] = [
    ToolSpec(
        "propose_memory",
        "Suggest a durable fact about the learner to remember. It stays 'proposed' until the learner confirms.",
        ProposeMemory.model_json_schema(),
    ),
    ToolSpec(
        "request_check",
        "Run the trusted checker for the current step. Call it when the learner says, in any words, that they "
        "have finished, or when they answer the step's question. Put ONLY the answer itself in `answer`, in "
        "their wording and without the rest of the sentence: “I guess it's clang on my Mac” gives `clang`.",
        RequestCheck.model_json_schema(),
    ),
]


def validate(call: ToolCall) -> ProposeMemory | RequestCheck | AssessStep | StartStep | InterpretReply | None:
    models: dict[str, type[BaseModel]] = {
        "propose_memory": ProposeMemory,
        "request_check": RequestCheck,
        "assess_step": AssessStep,
        "start_step": StartStep,
        "interpret_reply": InterpretReply,
    }
    model = models.get(call.name)
    if model is None:
        return None
    try:
        parsed: Any = model.model_validate(call.arguments)
    except ValidationError:
        return None
    return parsed  # type: ignore[no-any-return]
