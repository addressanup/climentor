"""Detect advice that went stale while it was being generated.

Each tutor request records the revision of the state keys it depended on.
When those keys change in a *material* way, unsolicited advice is withdrawn
and solicited answers get a visible "may be out of date" note.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..state.projection import WorkspaceState
from .policy import SOLICITED, Trigger


@dataclass(frozen=True)
class ContextRevision:
    revision: int
    keys: dict[str, int]
    exercise_dir: str | None = None
    failing_command: str | None = None


@dataclass(frozen=True)
class Staleness:
    changed: bool
    material: bool
    reason: str = ""


def relevant_keys(exercise_files: list[str]) -> list[str]:
    keys = ["shell.commands", "shell.last_exit", "exercise", "editor.saved"]
    keys += [f"buffer:{p}" for p in exercise_files]
    return keys


def assess(trigger: Trigger, snap: ContextRevision, state: WorkspaceState) -> Staleness:
    changed = [k for k, rev in snap.keys.items() if state.key_revision(k) > rev]
    if not changed:
        return Staleness(False, False)
    if "exercise" in changed:
        return Staleness(True, True, "the exercise status changed (a check ran)")
    if trigger == Trigger.REPEATED_FAILURE and "shell.commands" in changed:
        # Did the learner run a successful command in the exercise folder since?
        for record in reversed(state.commands):
            if record.finished_at is None:
                continue
            if snap.exercise_dir and not record.cwd.startswith(snap.exercise_dir):
                continue
            if record.exit_status == 0:
                return Staleness(True, True, f"you ran `{record.command}` successfully")
            break
    buffers = [k for k in changed if k.startswith("buffer:")]
    if trigger in SOLICITED:
        what = "your files" if buffers else "your shell"
        return Staleness(True, True, f"{what} changed while I was answering")
    return Staleness(True, False, "unrelated changes")
