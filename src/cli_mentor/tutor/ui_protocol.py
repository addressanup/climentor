"""How the engine talks to a user interface (Textual app or a headless test UI)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

OBSERVATION_WORDS = {"on": "● observing", "paused": "‖ paused", "off": "○ observation off"}
SOURCE_NAMES = {"term": "terminal", "shell": "shell", "nvim": "Neovim"}


@dataclass
class StatusInfo:
    goal: str | None
    exercise: str | None
    stage: str | None  # explain | attempt | verified
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
    phase: str = "ask_goal"  # ask_goal | plan_review | learning
    subject: str | None = None  # the plan's title
    step: int | None = None  # current step number (1-based)
    steps: int = 0  # steps in the plan
    done: int = 0  # steps done or skipped
    step_title: str | None = None
    step_stage: str | None = None  # the stage of the current step, None if it has not started

    @property
    def observation_words(self) -> str:
        return OBSERVATION_WORDS.get(self.observation, self.observation)

    @property
    def model_words(self) -> str:
        if self.mock:
            return "offline mock"
        # OpenAI asks apps to say when the learner's ChatGPT plan is paying.
        return f"{self.model} · using ChatGPT plan" if self.provider == "chatgpt" else (self.model or self.provider)

    def problems(self) -> list[str]:
        """Sources that are connected but not in sync, in words (empty when all is well)."""
        if self.observation != "on":
            return []
        return [
            f"{SOURCE_NAMES.get(k, k)} {v.replace('-', ' ')}"
            for k, v in self.sources.items()
            if v not in ("current", "none", "paused")
        ]

    def line(self) -> str:
        """Short plain-text status for the tmux status bar (words, so colour is never needed)."""
        parts = [self.subject or ("reviewing a plan" if self.phase == "plan_review" else "new subject")]
        if self.step is not None and self.steps:
            parts.append(f"step {self.step}/{self.steps}")
        parts += [self.observation_words, *self.problems(), self.model_words]
        if self.busy:
            parts.append("writing…")
        return " · ".join(parts)


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
