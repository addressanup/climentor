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


TOOLS: list[ToolSpec] = [
    ToolSpec(
        "propose_memory",
        "Suggest a durable fact about the learner to remember. It stays 'proposed' until the learner confirms.",
        ProposeMemory.model_json_schema(),
    ),
    ToolSpec(
        "request_check",
        "Ask the system to run the trusted checker for the current exercise.",
        RequestCheck.model_json_schema(),
    ),
]


def validate(call: ToolCall) -> ProposeMemory | RequestCheck | None:
    models: dict[str, type[BaseModel]] = {
        "propose_memory": ProposeMemory,
        "request_check": RequestCheck,
    }
    model = models.get(call.name)
    if model is None:
        return None
    try:
        parsed: Any = model.model_validate(call.arguments)
    except ValidationError:
        return None
    return parsed  # type: ignore[no-any-return]
