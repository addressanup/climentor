"""Trusted verifiers for the Neovim fundamentals track.

Final file contents alone do not prove *how* the learner edited. Technique is
reported as "verified" only with matching evidence (mode-change events, or
opt-in per-exercise command counts); otherwise "unverified".
"""

from __future__ import annotations

import os
from typing import ClassVar

from ...state.projection import BufferMirror
from ..verify import Check, Technique, VerificationResult, VerifyContext, register

MOTION_KEYS = {
    "w",
    "b",
    "e",
    "W",
    "B",
    "E",
    "f",
    "t",
    "F",
    "T",
    "/",
    "?",
    "G",
    "gg",
    "0",
    "$",
    "^",
    "n",
    "N",
    "{",
    "}",
}


def _buffer(ctx: VerifyContext, relative: str) -> BufferMirror | None:
    return ctx.state.buffer_for_path(os.path.realpath(ctx.workdir / relative))


def _saved_check(ctx: VerifyContext, relative: str, disk: str | None) -> Check:
    mirror = _buffer(ctx, relative)
    if mirror is None:
        return Check(
            "Saved with :w",
            disk is not None,
            "file is on disk (buffer not open in Neovim)",
            "filesystem",
        )
    saved = not mirror.modified and disk is not None and mirror.text.rstrip("\n") == disk.rstrip("\n")
    return Check(
        "Saved with :w",
        saved,
        "Neovim buffer and file on disk match"
        if saved
        else "Neovim has unsaved changes — press Esc, then type :w and Enter",
        "editor_state",
    )


@register
class ModesVerifier:
    id: ClassVar[str] = "nvim.modes.v1"
    executes_learner_code: ClassVar[bool] = False

    async def verify(self, ctx: VerifyContext) -> VerificationResult:
        rel = ctx.params.get("file", "modes.txt")
        line = ctx.params["line"]
        disk = ctx.read(rel)
        has_line = disk is not None and any(row.strip() == line for row in disk.splitlines())
        checks = [
            Check(
                f"modes.txt contains “{line}”",
                has_line,
                "found on disk" if has_line else "the line is not in the saved file yet",
                "filesystem",
            ),
            _saved_check(ctx, rel, disk),
        ]
        modes = [m for t, m in ctx.state.editor.mode_history if t >= ctx.attempt_started_at - 1]
        went_insert = False
        came_back = False
        for m in modes:
            if m.startswith("i"):
                went_insert = True
            elif went_insert and m.startswith("n"):
                came_back = True
        technique: Technique = "verified" if (went_insert and came_back) else "unverified"
        return VerificationResult(
            self.id,
            all(c.passed for c in checks),
            checks,
            technique=technique,
            technique_detail="saw Normal → Insert → Normal mode changes"
            if technique == "verified"
            else "did not observe Insert mode followed by Normal mode in this attempt",
        )


@register
class MotionsVerifier:
    id: ClassVar[str] = "nvim.motions.v1"
    executes_learner_code: ClassVar[bool] = False

    async def verify(self, ctx: VerifyContext) -> VerificationResult:
        rel = ctx.params.get("file", "motions.txt")
        line_no = int(ctx.params["line"])  # 1-based
        word = str(ctx.params["word"])
        mirror = _buffer(ctx, rel)
        cursor = ctx.state.editor.cursor
        on_word = False
        detail = "open motions.txt in Neovim first"
        if mirror is not None and cursor is not None and (cursor[0], cursor[1]) == (mirror.source_id, mirror.buf):
            row = cursor[2]
            col = cursor[3]
            text = mirror.lines[row] if row < len(mirror.lines) else ""
            start = text.find(word)
            on_word = row == line_no - 1 and start >= 0 and start <= col < start + len(word)
            detail = "cursor is on the word" if on_word else f"cursor is at line {row + 1}, column {col + 1}"
        checks = [Check(f"Cursor on {word} (line {line_no})", on_word, detail, "editor_state")]
        counts = ctx.state.editor.technique.get(ctx.exercise_id, {})
        used = sorted(k for k in counts if k in MOTION_KEYS)
        technique: Technique
        if not ctx.telemetry_enabled:
            technique, tdetail = (
                "unverified",
                "command telemetry is off for this exercise (your choice)",
            )
        elif used:
            technique, tdetail = "verified", "used motions: " + ", ".join(used)
        else:
            technique, tdetail = (
                "unverified",
                "no word/search motions seen (only arrow keys or hjkl?)",
            )
        return VerificationResult(self.id, on_word, checks, technique=technique, technique_detail=tdetail)


@register
class OperatorsVerifier:
    id: ClassVar[str] = "nvim.operators.v1"
    executes_learner_code: ClassVar[bool] = False

    async def verify(self, ctx: VerifyContext) -> VerificationResult:
        rel = ctx.params.get("file", "operators.txt")
        expected: dict[str, str] = ctx.params["expected_lines"]  # "2": "text"
        disk = ctx.read(rel)
        rows = disk.splitlines() if disk is not None else []
        checks = []
        for key, want in sorted(expected.items()):
            idx = int(key) - 1
            got = rows[idx] if idx < len(rows) else None
            ok = got == want
            checks.append(
                Check(
                    f"Line {key} reads “{want}”",
                    ok,
                    "correct" if ok else f"saved line is {got!r}",
                    "filesystem",
                )
            )
        checks.append(_saved_check(ctx, rel, disk))
        required: list[str] = list(ctx.params.get("required_commands", []))
        counts = ctx.state.editor.technique.get(ctx.exercise_id, {})
        missing = [c for c in required if counts.get(c, 0) < 1]
        technique: Technique
        if not ctx.telemetry_enabled:
            technique, tdetail = (
                "unverified",
                "command telemetry is off for this exercise (your choice)",
            )
        elif missing:
            technique, tdetail = "unverified", "not seen: " + ", ".join(missing)
        else:
            technique, tdetail = "verified", "saw " + ", ".join(required)
        return VerificationResult(
            self.id,
            all(c.passed for c in checks),
            checks,
            technique=technique,
            technique_detail=tdetail,
        )
