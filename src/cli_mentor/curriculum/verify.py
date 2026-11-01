"""Trusted verification framework.

Verifiers are Python classes registered by id. A lesson file can only *name*
a verifier and pass data parameters; it cannot add code or commands.
A model's opinion is never a verification result.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol

from ..observe.workspace import WorkspaceScope
from ..state.projection import CommandRecord, WorkspaceState
from .runner import HostRunner

Evidence = Literal[
    "filesystem",
    "shell_record",
    "editor_state",
    "editor_events",
    "executed_code",
    "learner_answer",
    "static_analysis",
]
Technique = Literal["verified", "unverified", "not_applicable"]


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str
    evidence: Evidence


@dataclass
class VerificationResult:
    verifier: str
    passed: bool
    checks: list[Check]
    technique: Technique = "not_applicable"
    technique_detail: str = ""
    executed_learner_code: bool = False
    needs_review: bool = False  # no automatic check: the tutor reviews the work
    at: float = field(default_factory=time.time)

    def summary(self) -> str:
        if self.needs_review:
            return (
                f"**No automatic check for this step** (trusted checker `{self.verifier}`). "
                "The tutor will review your work; that review is recorded as *unverified*."
            )
        relevant = self.checks if self.passed else [c for c in self.checks if not c.passed][:1]
        lines = [f"{c.name}: {c.detail}." for c in relevant]
        if not lines:
            lines.append("The work matches the task." if self.passed else "Let's take another look at the work.")
        if self.technique != "not_applicable":
            lines.append(self.technique_detail.rstrip(".") + ".")
        return " ".join(lines)


@dataclass
class VerifyContext:
    workdir: Path
    scope: WorkspaceScope
    state: WorkspaceState
    params: dict[str, Any]
    attempt_started_at: float
    answer: str | None = None
    runner: HostRunner | None = None
    telemetry_enabled: bool = False
    exercise_id: str = ""

    def commands_since_start(self) -> list[CommandRecord]:
        return [
            c for c in self.state.commands if c.finished_at is not None and c.finished_at >= self.attempt_started_at - 1
        ]

    def realpath(self, p: str | Path) -> str:
        return os.path.realpath(p)

    def read(self, relative: str) -> str | None:
        # Exact text: a correct `password = "..."` line must not fail because the secret filter changed it.
        return self.scope.read_text(self.workdir / relative, redact_secrets=False).text


class Verifier(Protocol):
    id: ClassVar[str]
    executes_learner_code: ClassVar[bool]

    async def verify(self, ctx: VerifyContext) -> VerificationResult: ...


REGISTRY: dict[str, Verifier] = {}


def register(cls: type[Any]) -> type[Any]:
    instance = cls()
    REGISTRY[instance.id] = instance
    return cls


def get_verifier(verifier_id: str) -> Verifier:
    load_adapters()
    try:
        return REGISTRY[verifier_id]
    except KeyError:
        raise KeyError(f"unknown verifier {verifier_id!r}") from None


def load_adapters() -> None:
    from .adapters import generic  # noqa: F401  (registers verifiers)
