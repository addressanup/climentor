"""The trusted checker for AI-prepared plan steps (``plan.generic.v1``).

It only runs checks from the fixed menu in ``curriculum/plans.py``. The model
chooses which checks and their data; this code decides what actually runs.
Programs are compiled with fixed flags and run with limits (host execution,
not a sandbox), and only after the learner allowed running their code.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import ClassVar

from ..plans import (
    CHECKS_ADAPTER,
    CommandSucceeded,
    EditorCommands,
    FileContains,
    FileExists,
    LearnerAnswer,
    ProgramOutput,
)
from ..verify import Check, Technique, VerificationResult, VerifyContext, register


def _normalise_output(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").strip("\n").split("\n"))


def _normalise_answer(text: str) -> str:
    return re.sub(r"[\s\W_]+", " ", text.casefold()).strip()


def _first_word(command: str) -> str:
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
        words = words[1:]  # skip VAR=value prefixes
    return os.path.basename(words[0]) if words else ""


@register
class PlanStepVerifier:
    id: ClassVar[str] = "plan.generic.v1"
    executes_learner_code: ClassVar[bool] = False  # the engine uses the lesson's risk instead

    async def verify(self, ctx: VerifyContext) -> VerificationResult:
        checks = CHECKS_ADAPTER.validate_python(ctx.params.get("checks", []))
        results: list[Check] = []
        technique: Technique = "not_applicable"
        technique_detail = ""
        executed = False
        for c in checks:
            if isinstance(c, FileExists):
                ok = ctx.scope.check(ctx.workdir / c.path).allowed and (ctx.workdir / c.path).is_file()
                results.append(Check(f"{c.path} exists", ok, "found" if ok else "not found", "filesystem"))
            elif isinstance(c, FileContains):
                text = ctx.read(c.path)
                ok = text is not None and c.text in text
                detail = "found" if ok else ("file not found" if text is None else f"“{c.text}” is not in the file yet")
                results.append(Check(f"{c.path} contains “{c.text}”", ok, detail, "filesystem"))
            elif isinstance(c, CommandSucceeded):
                seen = [r for r in ctx.commands_since_start() if _first_word(r.command) == c.program]
                ok = any(r.exit_status == 0 for r in seen)
                detail = (
                    f"saw `{next(r for r in seen if r.exit_status == 0).command}`"
                    if ok
                    else (
                        f"`{c.program}` ran but ended with an error (exit {seen[-1].exit_status})"
                        if seen
                        else f"no `{c.program}` command observed since the step started"
                    )
                )
                results.append(Check(f"`{c.program}` ran successfully", ok, detail, "shell_record"))
            elif isinstance(c, ProgramOutput):
                if ctx.runner is None:
                    results.append(
                        Check(f"{c.source} output", False, "running your code was not allowed", "executed_code")
                    )
                    continue
                if not ctx.scope.check(ctx.workdir / c.source).allowed or not (ctx.workdir / c.source).is_file():
                    results.append(Check(f"{c.source} output", False, f"{c.source} not found", "executed_code"))
                    continue
                executed = True
                run, stage = await ctx.runner.run_program(c.language, c.source, ctx.workdir, c.stdin)
                if run.timed_out:
                    ok, detail = False, f"{stage} timed out (infinite loop?)"
                elif not run.ok:
                    last = run.stderr.strip().splitlines() or [f"exit {run.exit_code}"]
                    ok, detail = (
                        False,
                        f"{'did not compile' if stage == 'compile' else 'crashed'}: " + " | ".join(last[:3]),
                    )
                else:
                    ok = _normalise_output(run.stdout) == _normalise_output(c.expected_stdout)
                    detail = "output matches" if ok else f"printed {run.stdout.strip()[:200]!r}"
                results.append(Check(f"{c.source} prints the expected output", ok, detail, "executed_code"))
            elif isinstance(c, EditorCommands):
                counts = ctx.state.editor.technique.get(ctx.exercise_id, {})
                missing = [x for x in c.commands if counts.get(x, 0) < 1]
                if not ctx.telemetry_enabled:
                    technique, technique_detail = "unverified", "command counting is off for this step (your choice)"
                elif missing:
                    technique, technique_detail = "unverified", "not seen: " + ", ".join(missing)
                else:
                    technique, technique_detail = "verified", "saw " + ", ".join(c.commands)
            elif isinstance(c, LearnerAnswer):
                given = _normalise_answer(ctx.answer or "")
                ok = bool(given) and given in {_normalise_answer(a) for a in c.accepted}
                detail = "correct" if ok else ("tell me your answer" if not given else "not the expected answer")
                results.append(Check(f"Answer: {c.question}", ok, detail, "learner_answer"))
        if not results:
            return VerificationResult(self.id, False, [], technique, technique_detail, executed, needs_review=True)
        return VerificationResult(
            self.id, all(r.passed for r in results), results, technique, technique_detail, executed
        )
