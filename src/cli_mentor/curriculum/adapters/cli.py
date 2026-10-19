"""Trusted verifiers for the CLI fundamentals track."""

from __future__ import annotations

import random
import re
import shlex
from pathlib import Path
from typing import Any, ClassVar

from ..verify import Check, VerificationResult, VerifyContext, generator, register


def _words(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


@generator("log_lines")
def log_lines(params: dict[str, Any]) -> str:
    """A deterministic fake server log."""
    rng = random.Random(int(params.get("seed", 7)))
    n = int(params.get("lines", 60))
    services = ["auth", "db", "web", "cache", "queue"]
    messages = {
        "INFO": ["request served", "user logged in", "cache warmed", "job finished"],
        "WARN": ["slow query", "retrying connection", "disk 80% full"],
        "ERROR": ["timeout talking to db", "payment declined", "file not found", "out of memory"],
    }
    out = []
    for i in range(n):
        level = rng.choices(["INFO", "WARN", "ERROR"], weights=[6, 2, 2])[0]
        svc = rng.choice(services)
        out.append(f"2026-10-19T09:{i // 60:02d}:{i % 60:02d} {level:<5} [{svc}] {rng.choice(messages[level])}")
    return "\n".join(out) + "\n"


@register
class NavigationVerifier:
    id: ClassVar[str] = "cli.navigation.v1"
    executes_learner_code: ClassVar[bool] = False

    async def verify(self, ctx: VerifyContext) -> VerificationResult:
        target_dir = ctx.realpath(ctx.workdir / ctx.params["target_dir"])
        target_file = str(ctx.params["target_file"])
        secret = str(ctx.params["secret"]).lower()
        cmds = ctx.commands_since_start()
        visited = any(ctx.realpath(c.cwd) == target_dir and c.exit_status == 0 for c in cmds)
        read_file = any(
            c.exit_status == 0
            and _words(c.command)[:1] == ["cat"]
            and any(w.endswith(target_file) for w in _words(c.command)[1:])
            for c in cmds
        )
        answer = (ctx.answer or "").strip().lower()
        checks = [
            Check(
                "You went into the folder with cd",
                visited,
                f"a command finished while your shell was in {ctx.params['target_dir']}"
                if visited
                else f"I did not see your shell inside {ctx.params['target_dir']} yet",
                "shell_record",
            ),
            Check(
                "You read the file with cat",
                read_file,
                f"saw a successful `cat ...{target_file}`"
                if read_file
                else f"no successful `cat` of {target_file} observed",
                "shell_record",
            ),
            Check(
                "You found the secret word",
                answer == secret,
                "correct"
                if answer == secret
                else (
                    "type `/check <word>` with the word from the file"
                    if not answer
                    else "that is not the word in the file"
                ),
                "learner_answer",
            ),
        ]
        if ctx.state.shell_history_incomplete and not (visited and read_file):
            checks.append(
                Check(
                    "Observation completeness",
                    False,
                    "some shell events were missed (gap); run the commands again",
                    "shell_record",
                )
            )
        return VerificationResult(self.id, all(c.passed for c in checks), checks)


@register
class FilesVerifier:
    id: ClassVar[str] = "cli.files.v1"
    executes_learner_code: ClassVar[bool] = False

    async def verify(self, ctx: VerifyContext) -> VerificationResult:
        d: Path = ctx.workdir / "desk"
        notes = d / "notes"
        final = notes / "final.txt"
        draft = d / "draft.txt"
        checks = [
            Check(
                "Folder desk/notes exists",
                notes.is_dir(),
                "found" if notes.is_dir() else "missing — try `mkdir`",
                "filesystem",
            ),
        ]
        draft_text = ctx.read("desk/draft.txt")
        final_text = ctx.read("desk/notes/final.txt")
        copied = final.is_file() and draft.is_file() and draft_text is not None and draft_text == final_text
        checks.append(
            Check(
                "draft.txt copied to notes/final.txt",
                copied,
                "both files exist with the same text"
                if copied
                else (
                    "draft.txt is gone — `cp` copies, `mv` moves"
                    if not draft.is_file()
                    else "notes/final.txt missing or different"
                ),
                "filesystem",
            )
        )
        archive = d / "archive.txt"
        renamed = (
            archive.is_file()
            and not (d / "old-name.txt").exists()
            and (ctx.read("desk/archive.txt") or "").strip() == "rename me"
        )
        checks.append(
            Check(
                "old-name.txt renamed to archive.txt",
                renamed,
                "renamed" if renamed else "expected archive.txt (with the old text) and no old-name.txt",
                "filesystem",
            )
        )
        junk_gone = not (d / "junk.tmp").exists()
        checks.append(
            Check(
                "junk.tmp removed",
                junk_gone,
                "removed" if junk_gone else "still there",
                "filesystem",
            )
        )
        used_i = any(
            c.exit_status == 0 and re.search(r"(^|\s)rm\s+(-\w*i\w*\s+)+.*junk\.tmp", c.command)
            for c in ctx.commands_since_start()
        )
        return VerificationResult(
            self.id,
            all(c.passed for c in checks),
            checks,
            technique="verified" if used_i else "unverified",
            technique_detail="saw `rm -i ... junk.tmp`"
            if used_i
            else "did not observe `rm -i` for junk.tmp (safe deletion habit not shown)",
        )


@register
class PipelinesVerifier:
    id: ClassVar[str] = "cli.pipelines.v1"
    executes_learner_code: ClassVar[bool] = False

    async def verify(self, ctx: VerifyContext) -> VerificationResult:
        log_text = ctx.read(ctx.params.get("log", "server.log"))
        out_name = ctx.params.get("output", "error_count.txt")
        word = ctx.params.get("word", "ERROR")
        checks: list[Check] = []
        if log_text is None:
            checks.append(
                Check(
                    "server.log readable",
                    False,
                    "server.log is missing; run `/lesson reset`",
                    "filesystem",
                )
            )
            return VerificationResult(self.id, False, checks)
        expected = sum(1 for line in log_text.splitlines() if word in line)
        out_text = ctx.read(out_name)
        got = (out_text or "").strip()
        correct = got.isdigit() and int(got) == expected
        checks.append(
            Check(
                f"{out_name} holds the right count",
                correct,
                "correct number"
                if correct
                else (
                    f"{out_name} not found"
                    if out_text is None
                    else f"found {got!r}, which is not the number of lines containing {word}"
                ),
                "filesystem",
            )
        )
        pipeline = next(
            (
                c
                for c in reversed(ctx.commands_since_start())
                if c.exit_status == 0 and "|" in c.command and ">" in c.command and "grep" in c.command
            ),
            None,
        )
        checks.append(
            Check(
                "You used a pipeline with a redirect",
                pipeline is not None,
                f"saw `{pipeline.command}`"
                if pipeline
                else "no successful `grep ... | ... > ...` command observed (if observation was paused, run it again)",
                "shell_record",
            )
        )
        return VerificationResult(self.id, all(c.passed for c in checks), checks)
