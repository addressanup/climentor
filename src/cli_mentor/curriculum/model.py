"""Versioned lesson format (format version 1).

Lesson files are data only. They never contain commands to run. Each lesson
names a *verifier id*; the verifier is trusted Python code in ``adapters/``.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

FORMAT_VERSION = 1


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Vocab(_M):
    term: str
    meaning: str


class SyntaxPart(_M):
    token: str
    meaning: str


class SyntaxBreakdown(_M):
    example: str
    parts: list[SyntaxPart]
    note: str = ""


class SetupFile(_M):
    path: str
    kind: Literal["file", "dir"] = "file"
    content: str = ""
    generator: str | None = None  # name of a trusted generator in adapters
    generator_params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("path")
    @classmethod
    def _safe_relative(cls, value: str) -> str:
        p = PurePosixPath(value)
        if p.is_absolute() or ".." in p.parts or not p.parts:
            raise ValueError(f"setup path must be relative and stay inside the exercise: {value}")
        return value


class Hint(_M):
    level: int = Field(ge=1, le=3)
    text: str


class Lesson(_M):
    format_version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-z0-9]+(\.[a-z0-9_]+)+$")
    version: int = Field(ge=1)
    title: str
    track: Literal["cli", "nvim", "python"] | str
    order: int = 0
    skills: list[str] = Field(min_length=1)
    prerequisites: list[str] = Field(default_factory=list)
    objectives: list[str] = Field(min_length=1)
    vocabulary: list[Vocab] = Field(default_factory=list)
    syntax: list[SyntaxBreakdown] = Field(default_factory=list)
    demonstration: str = ""
    prediction: str = ""
    prediction_answer: str = ""
    instructions: str
    workdir: str
    setup: list[SetupFile] = Field(default_factory=list)
    hints: list[Hint] = Field(min_length=1)
    solution: str = ""
    verifier: str
    verifier_params: dict[str, Any] = Field(default_factory=dict)
    rubric: list[str] = Field(default_factory=list)
    transfer: list[str] = Field(default_factory=list)
    cleanup: Literal["keep", "remove_created"] = "keep"
    risk: Literal["read_only", "writes_workspace", "runs_learner_code"]
    telemetry: Literal["none", "nvim_normal_commands"] = "none"
    estimated_minutes: int = 10

    @field_validator("workdir")
    @classmethod
    def _safe_workdir(cls, value: str) -> str:
        p = PurePosixPath(value)
        if p.is_absolute() or ".." in p.parts or not p.parts:
            raise ValueError("workdir must be a relative folder inside the workspace")
        return value

    def hint(self, level: int) -> Hint | None:
        for h in sorted(self.hints, key=lambda h: h.level):
            if h.level == level:
                return h
        return None

    @property
    def max_hint_level(self) -> int:
        return max(h.level for h in self.hints)

    def explanation_markdown(self) -> str:
        """Vocabulary and symbol-by-symbol syntax, rendered from lesson data."""
        out: list[str] = []
        if self.objectives:
            out.append("**You will learn:**\n" + "\n".join(f"- {o}" for o in self.objectives))
        if self.vocabulary:
            out.append("**New words:**\n" + "\n".join(f"- **{v.term}** — {v.meaning}" for v in self.vocabulary))
        for s in self.syntax:
            lines = [f"**Reading `{s.example}` piece by piece:**"]
            lines += [f"- `{p.token}` — {p.meaning}" for p in s.parts]
            if s.note:
                lines.append(f"\n{s.note}")
            out.append("\n".join(lines))
        if self.demonstration:
            out.append("**Example:**\n\n" + self.demonstration)
        return "\n\n".join(out)

    def lookup_term(self, term: str) -> str | None:
        t = term.strip().strip("`").lower()
        for v in self.vocabulary:
            if v.term.lower() == t:
                return f"**{v.term}** — {v.meaning}"
        for s in self.syntax:
            for p in s.parts:
                if p.token.lower() == t:
                    return f"`{p.token}` (from `{s.example}`) — {p.meaning}"
        return None
