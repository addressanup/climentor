"""How the engine talks to a user interface (Textual app or a headless test UI)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class StatusInfo:
    goal: str | None
    exercise: str | None
    stage: str | None
    mode: str
    observation: str  # "on" | "paused" | "off"
    sync: str
    sources: dict[str, str]
    provider: str
    model: str
    mock: bool
    permissions: list[str]
    busy: bool
    usage: dict[str, int] = field(default_factory=dict)

    def line(self) -> str:
        """Plain-text status (no colour needed to understand it)."""
        src = " ".join(f"{k}:{v}" for k, v in self.sources.items())
        ex = f"{self.exercise} ({self.stage})" if self.exercise else "none"
        model = "MOCK" if self.mock else f"{self.provider}/{self.model or '?'}"
        busy = " | TUTOR: writing" if self.busy else ""
        return (
            f"GOAL: {self.goal or '-'} | EX: {ex} | OBS: {self.observation.upper()} | "
            f"SYNC: {self.sync} [{src}] | MODE: {self.mode} | MODEL: {model}{busy}"
        )


class UiSink(Protocol):
    def tutor_begin(self, turn_id: int, trigger: str, label: str) -> None: ...

    def tutor_delta(self, turn_id: int, text: str) -> None: ...

    def tutor_end(self, turn_id: int, status: str, note: str = "") -> None: ...

    def note(self, text: str, kind: str = "info") -> None: ...

    def status(self, info: StatusInfo) -> None: ...

    async def ask(self, title: str, body: str, choices: list[tuple[str, str]]) -> str: ...


@dataclass
class Turn:
    turn_id: int
    trigger: str
    label: str
    text: str = ""
    status: str = "streaming"
    note: str = ""


class HeadlessUi:
    """Records everything; answers questions from a script (default: first choice)."""

    def __init__(self, answers: dict[str, str] | None = None) -> None:
        self.turns: dict[int, Turn] = {}
        self.notes: list[tuple[str, str]] = []
        self.statuses: list[StatusInfo] = []
        self.questions: list[tuple[str, str]] = []
        self.answers = answers or {}
        self.default_answer: str | None = None

    def tutor_begin(self, turn_id: int, trigger: str, label: str) -> None:
        self.turns[turn_id] = Turn(turn_id, trigger, label)

    def tutor_delta(self, turn_id: int, text: str) -> None:
        self.turns[turn_id].text += text

    def tutor_end(self, turn_id: int, status: str, note: str = "") -> None:
        self.turns[turn_id].status = status
        self.turns[turn_id].note = note

    def note(self, text: str, kind: str = "info") -> None:
        self.notes.append((kind, text))

    def status(self, info: StatusInfo) -> None:
        self.statuses.append(info)

    async def ask(self, title: str, body: str, choices: list[tuple[str, str]]) -> str:
        self.questions.append((title, body))
        for key, answer in self.answers.items():
            if key.lower() in title.lower():
                return answer
        return self.default_answer or choices[0][0]

    # helpers for tests
    def last_turn(self, trigger: str | None = None) -> Turn | None:
        items = [t for t in self.turns.values() if trigger is None or t.trigger == trigger]
        return items[-1] if items else None

    def all_text(self) -> str:
        return "\n".join(t.text for t in self.turns.values()) + "\n" + "\n".join(n for _k, n in self.notes)
