"""Deterministic intervention policy: when the tutor may speak.

The model never decides when to interrupt. This class does, from:
explicit learner requests, mode, quiet periods (debounce), cooldowns,
repeated-failure checkpoints, and "no overlapping unsolicited responses".
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class Trigger(StrEnum):
    LEARNER_MESSAGE = "learner_message"
    HINT = "hint"
    EXPLAIN = "explain"
    LESSON_INTRO = "lesson_intro"
    PREDICTION = "prediction"
    VERIFICATION = "verification"
    REVIEW = "review"
    SESSION_START = "session_start"
    REPEATED_FAILURE = "repeated_failure"


SOLICITED = frozenset(
    {
        Trigger.LEARNER_MESSAGE,
        Trigger.HINT,
        Trigger.EXPLAIN,
        Trigger.LESSON_INTRO,
        Trigger.PREDICTION,
        Trigger.VERIFICATION,
        Trigger.REVIEW,
        Trigger.SESSION_START,
    }
)


@dataclass(frozen=True)
class Decision:
    act: bool
    reason: str
    wait: float = 0.0  # >0: ask again after this many seconds (debounce)


@dataclass
class PolicySettings:
    cooldown: float = 60.0
    debounce: float = 2.0
    failure_threshold: int = 2


class InterventionPolicy:
    def __init__(self, settings: PolicySettings, clock: Callable[[], float] = time.monotonic) -> None:
        self.settings = settings
        self.clock = clock
        self.last_unsolicited: float | None = None
        self.consecutive_failures = 0
        self.last_failure_command: str | None = None

    # ------------------------------------------------------------ inputs
    def note_command(self, exit_status: int, relevant: bool, command: str) -> bool:
        """Track exercise-relevant command results. True when a failure checkpoint is reached."""
        if not relevant:
            return False
        if exit_status == 0:
            self.consecutive_failures = 0
            self.last_failure_command = None
            return False
        self.consecutive_failures += 1
        self.last_failure_command = command
        return self.consecutive_failures >= self.settings.failure_threshold

    def reset_failures(self) -> None:
        self.consecutive_failures = 0
        self.last_failure_command = None

    # ------------------------------------------------------------ decision
    def decide(
        self,
        trigger: Trigger,
        *,
        mode: str,
        style: str = "balanced",
        response_in_flight: bool,
        shell_busy: bool,
        seconds_since_activity: float,
        exercise_active: bool,
    ) -> Decision:
        if trigger in SOLICITED:
            return Decision(True, "learner asked" if trigger != Trigger.VERIFICATION else "check finished")
        if mode in ("quiet", "assessment") or style == "quiet":
            return Decision(
                False,
                f"{mode if mode in ('quiet', 'assessment') else 'quiet style'}: only speak when asked",
            )
        if not exercise_active and mode != "project":
            return Decision(False, "no active exercise")
        if response_in_flight:
            return Decision(False, "another response is in progress")
        now = self.clock()
        cooldown = self.settings.cooldown * (0.5 if style == "proactive" else 1.0)
        if self.last_unsolicited is not None and now - self.last_unsolicited < cooldown:
            return Decision(False, f"cooldown ({cooldown - (now - self.last_unsolicited):.0f}s left)")
        if shell_busy:
            return Decision(False, "a command is still running", wait=self.settings.debounce)
        if seconds_since_activity < self.settings.debounce:
            return Decision(
                False,
                "waiting for a quiet moment",
                wait=self.settings.debounce - seconds_since_activity,
            )
        return Decision(True, "repeated failure checkpoint")

    def mark_unsolicited(self) -> None:
        self.last_unsolicited = self.clock()
        self.consecutive_failures = 0
