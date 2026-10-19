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
    # The learner's answer to the step's question, in a few words of their own (trusted code compares it).
    answer: str = Field(default="", max_length=200)


class StartStep(BaseModel):
    """The learner said, in their own words, that they are ready for the next step."""

    model_config = ConfigDict(extra="forbid")


class AssessStep(BaseModel):
    """The tutor's review of a step that has no automatic check (recorded as unverified)."""

    model_config = ConfigDict(extra="forbid")
    verdict: Literal["complete", "needs_work"]
    feedback: str = Field(default="", max_length=600)


ASSESS_TOOL = ToolSpec(
    "assess_step",
    "Give your verdict on the learner's work for a step that has no automatic check: 'complete' or "
    "'needs_work', with one or two sentences of feedback. Your verdict is recorded as unverified.",
    AssessStep.model_json_schema(),
)

START_TOOL = ToolSpec(
    "start_step",
    "Start the next step of the plan. Call it when the learner says, in any words, that they are ready to go on.",
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
        "have finished, or when they answer the step's question. Put their answer in `answer` (a few words, "
        "their own wording, for example `int`).",
        RequestCheck.model_json_schema(),
    ),
]


def validate(call: ToolCall) -> ProposeMemory | RequestCheck | AssessStep | StartStep | None:
    models: dict[str, type[BaseModel]] = {
        "propose_memory": ProposeMemory,
        "request_check": RequestCheck,
        "assess_step": AssessStep,
        "start_step": StartStep,
    }
    model = models.get(call.name)
    if model is None:
        return None
    try:
        parsed: Any = model.model_validate(call.arguments)
    except ValidationError:
        return None
    return parsed  # type: ignore[no-any-return]
