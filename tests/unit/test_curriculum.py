from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from cli_mentor.curriculum.loader import find_lesson, lessons_by_track, load_lessons, next_lesson
from cli_mentor.curriculum.model import Lesson
from cli_mentor.curriculum.runner import HostRunner
from cli_mentor.curriculum.setup import apply_setup, cleanup, plan_setup
from cli_mentor.curriculum.verify import VerifyContext, get_verifier
from cli_mentor.db.store import open_store
from cli_mentor.events.schema import (
    EventEnvelope,
    NvimBufferSnapshot,
    NvimCursor,
    NvimMode,
    NvimTechnique,
    Payload,
    ShellCommandFinished,
    SourceKind,
    SourceRef,
)
from cli_mentor.observe.workspace import WorkspaceScope
from cli_mentor.state.projection import WorkspaceState

T0 = time.time()


def lesson(lid: str) -> Lesson:
    found = load_lessons()[lid]
    return found


def test_all_lessons_valid_and_three_per_track() -> None:
    tracks = lessons_by_track()
    assert list(tracks) == ["cli", "nvim", "python"]
    for items in tracks.values():
        assert len(items) >= 3
    for les in load_lessons().values():
        md = les.explanation_markdown()
        assert "**You will learn:**" in md
        assert les.vocabulary or les.syntax, les.id
        assert all(h.text for h in les.hints)
        assert les.hint(1) is not None


def test_lesson_lookup_and_order() -> None:
    assert find_lesson("navigation") is lesson("cli.navigation")
    assert next_lesson(set()) is lesson("cli.navigation")
    assert next_lesson({"cli.navigation"}, track="cli") is lesson("cli.files")
    assert lesson("cli.pipelines").lookup_term("|") is not None
    assert lesson("cli.navigation").lookup_term("working directory") is not None


def test_lesson_paths_cannot_escape() -> None:
    data: dict[str, Any] = lesson("cli.files").model_dump()
    data["setup"] = [{"path": "../../etc/evil", "content": "x"}]
    with pytest.raises(ValueError):
        Lesson.model_validate(data)
    data = lesson("cli.files").model_dump()
    data["workdir"] = "/tmp/abs"
    with pytest.raises(ValueError):
        Lesson.model_validate(data)


def test_setup_and_cleanup(tmp_path: Path, workspace: Path) -> None:
    store = open_store(tmp_path / "db.sqlite3")
    scope = WorkspaceScope([workspace])
    les = lesson("cli.pipelines")
    plan = plan_setup(les, workspace, scope)
    assert [p.name for p, _ in plan.to_create] == ["server.log"]
    apply_setup(plan, store)
    log = workspace / les.workdir / "server.log"
    assert "ERROR" in log.read_text()
    # Re-planning keeps existing files.
    again = plan_setup(les, workspace, scope)
    assert again.existing == [log] and not again.to_create
    # Learner file is not touched by cleanup; our unchanged file is removed.
    (workspace / les.workdir / "error_count.txt").write_text("5\n")
    report = dict(cleanup(les, workspace, store))
    assert report[str(log)] == "removed"
    assert (workspace / les.workdir / "error_count.txt").exists()


def test_cleanup_keeps_changed_files(tmp_path: Path, workspace: Path) -> None:
    store = open_store(tmp_path / "db.sqlite3")
    scope = WorkspaceScope([workspace])
    les = lesson("cli.navigation")
    apply_setup(plan_setup(les, workspace, scope), store)
    treasure = workspace / les.workdir / "maze/south/cellar/treasure.txt"
    treasure.write_text("I edited this\n")
    report = dict(cleanup(les, workspace, store))
    assert report[str(treasure)] == "kept (you changed it)"
    assert treasure.exists()
    assert not (workspace / les.workdir / "maze/north").exists()


# ---------------------------------------------------------------- verifiers
def finished(cmd: str, cwd: Path | str, status: int = 0, seq: int = 1) -> EventEnvelope:
    return mk(
        ShellCommandFinished(
            command=cmd,
            cwd=str(cwd),
            exit_status=status,
            started_at=T0 + seq,
            finished_at=T0 + seq + 0.1,
        ),
        SourceKind.SHELL,
        seq,
    )


def mk(payload: Payload, kind: SourceKind, seq: int, sid: str = "src-1") -> EventEnvelope:
    return EventEnvelope(
        session_id="s",
        source=SourceRef(kind=kind, id=sid, epoch="e"),
        seq=seq,
        captured_at=T0 + seq,
        received_at=T0 + seq,
        payload=payload,
    )


def ctx_for(les: Lesson, workspace: Path, state: WorkspaceState, **kw: Any) -> VerifyContext:
    return VerifyContext(
        workdir=workspace / les.workdir,
        scope=WorkspaceScope([workspace]),
        state=state,
        params=les.verifier_params,
        attempt_started_at=T0,
        exercise_id=les.id,
        **kw,
    )


def prepared(les: Lesson, tmp_path: Path, workspace: Path) -> Path:
    apply_setup(plan_setup(les, workspace, WorkspaceScope([workspace])), open_store(tmp_path / "v.sqlite3"))
    return workspace / les.workdir


async def test_navigation_verifier(tmp_path: Path, workspace: Path) -> None:
    les = lesson("cli.navigation")
    wd = prepared(les, tmp_path, workspace)
    st = WorkspaceState()
    v = get_verifier(les.verifier)
    result = await v.verify(ctx_for(les, workspace, st, answer="lantern"))
    assert not result.passed  # nothing observed yet
    st.apply(finished("cd maze/south/cellar", wd / "maze/south/cellar", seq=1))
    st.apply(finished("cat treasure.txt", wd / "maze/south/cellar", seq=2))
    assert not (await v.verify(ctx_for(les, workspace, st, answer="wrong"))).passed
    result = await v.verify(ctx_for(les, workspace, st, answer="Lantern"))
    assert result.passed, result.summary()


async def test_files_verifier(tmp_path: Path, workspace: Path) -> None:
    les = lesson("cli.files")
    wd = prepared(les, tmp_path, workspace)
    st = WorkspaceState()
    v = get_verifier(les.verifier)
    assert not (await v.verify(ctx_for(les, workspace, st))).passed
    (wd / "desk/notes").mkdir()
    (wd / "desk/notes/final.txt").write_text((wd / "desk/draft.txt").read_text())
    (wd / "desk/old-name.txt").rename(wd / "desk/archive.txt")
    (wd / "desk/junk.tmp").unlink()
    result = await v.verify(ctx_for(les, workspace, st))
    assert result.passed and result.technique == "unverified"
    st.apply(finished("rm -i desk/junk.tmp", wd, seq=3))
    assert (await v.verify(ctx_for(les, workspace, st))).technique == "verified"


async def test_pipelines_verifier(tmp_path: Path, workspace: Path) -> None:
    les = lesson("cli.pipelines")
    wd = prepared(les, tmp_path, workspace)
    expected = sum("ERROR" in line for line in (wd / "server.log").read_text().splitlines())
    st = WorkspaceState()
    v = get_verifier(les.verifier)
    (wd / "error_count.txt").write_text(f"{expected + 1}\n")
    st.apply(finished("grep ERROR server.log | wc -l > error_count.txt", wd, seq=1))
    assert not (await v.verify(ctx_for(les, workspace, st))).passed
    (wd / "error_count.txt").write_text(f"      {expected}\n")
    result = await v.verify(ctx_for(les, workspace, st))
    assert result.passed, result.summary()
    # Correct file but typed by hand: not a pass, the pipeline was not observed.
    st2 = WorkspaceState()
    st2.apply(finished(f"echo {expected} > error_count.txt", wd, seq=1))
    assert not (await v.verify(ctx_for(les, workspace, st2))).passed


def nvim_state(
    path: Path,
    lines: list[str],
    modified: bool,
    modes: list[str] = (),
    counts: dict[str, int] | None = None,
    cursor: tuple[int, int] | None = None,
    ex: str = "",
) -> WorkspaceState:  # type: ignore[assignment]
    st = WorkspaceState()
    st.apply(
        mk(
            NvimBufferSnapshot(buf=1, path=os.path.realpath(path), changedtick=5, modified=modified, lines=lines),
            SourceKind.NVIM,
            1,
        )
    )
    for i, m in enumerate(modes, start=2):
        st.apply(mk(NvimMode(mode=m), SourceKind.NVIM, i))
    if counts:
        st.apply(mk(NvimTechnique(exercise_id=ex, buf=1, counts=counts), SourceKind.NVIM, 50))
    if cursor:
        st.apply(mk(NvimCursor(buf=1, line=cursor[0], col=cursor[1]), SourceKind.NVIM, 51))
    return st


async def test_modes_verifier(tmp_path: Path, workspace: Path) -> None:
    les = lesson("nvim.modes")
    wd = prepared(les, tmp_path, workspace)
    f = wd / "modes.txt"
    lines = ["# Practice file for Neovim modes", "I can switch modes."]
    v = get_verifier(les.verifier)
    # Typed but not saved.
    st = nvim_state(f, lines, modified=True, modes=["n", "i", "n"])
    assert not (await v.verify(ctx_for(les, workspace, st))).passed
    f.write_text("\n".join(lines) + "\n")
    st = nvim_state(f, lines, modified=False, modes=["n", "i", "n"])
    result = await v.verify(ctx_for(les, workspace, st))
    assert result.passed and result.technique == "verified"
    st = nvim_state(f, lines, modified=False, modes=[])
    assert (await v.verify(ctx_for(les, workspace, st))).technique == "unverified"


async def test_motions_verifier(tmp_path: Path, workspace: Path) -> None:
    les = lesson("nvim.motions")
    wd = prepared(les, tmp_path, workspace)
    lines = (wd / "motions.txt").read_text().splitlines()
    col = lines[4].index("TARGET") + 2
    v = get_verifier(les.verifier)
    st = nvim_state(wd / "motions.txt", lines, False, cursor=(4, col), counts={"w": 3, "j": 4}, ex=les.id)
    r = await v.verify(ctx_for(les, workspace, st, telemetry_enabled=True))
    assert r.passed and r.technique == "verified"
    r = await v.verify(ctx_for(les, workspace, st, telemetry_enabled=False))
    assert r.passed and r.technique == "unverified"
    st = nvim_state(wd / "motions.txt", lines, False, cursor=(0, 0))
    assert not (await v.verify(ctx_for(les, workspace, st))).passed


async def test_operators_verifier(tmp_path: Path, workspace: Path) -> None:
    les = lesson("nvim.operators")
    wd = prepared(les, tmp_path, workspace)
    good = ["Fix these lines using operators.", "delete the extra word", "I like the color blue"]
    (wd / "operators.txt").write_text("\n".join(good) + "\n")
    v = get_verifier(les.verifier)
    st = nvim_state(wd / "operators.txt", good, False, counts={"dw": 1, "ciw": 1}, ex=les.id)
    r = await v.verify(ctx_for(les, workspace, st, telemetry_enabled=True))
    assert r.passed and r.technique == "verified"
    st = nvim_state(wd / "operators.txt", good, False, counts={"x": 9}, ex=les.id)
    r = await v.verify(ctx_for(les, workspace, st, telemetry_enabled=True))
    assert r.passed and r.technique == "unverified"


async def test_python_expressions_verifier(tmp_path: Path, workspace: Path) -> None:
    les = lesson("python.expressions")
    wd = prepared(les, tmp_path, workspace)
    v = get_verifier(les.verifier)
    runner = HostRunner()
    (wd / "expressions.py").write_text("print(12)\nprint(3)\nprint(1)\nprint(1024)\n")
    r = await v.verify(ctx_for(les, workspace, WorkspaceState(), runner=runner))
    assert not r.passed  # right output, but the operators were not used
    (wd / "expressions.py").write_text("print(7 + 5)\nprint(7 // 2)\nprint(7 % 2)\nprint(2 ** 10)\n")
    r = await v.verify(ctx_for(les, workspace, WorkspaceState(), runner=runner))
    assert r.passed and r.executed_learner_code, r.summary()


async def test_python_types_verifier_and_spoofing(tmp_path: Path, workspace: Path) -> None:
    les = lesson("python.types")
    wd = prepared(les, tmp_path, workspace)
    v = get_verifier(les.verifier)
    runner = HostRunner()
    (wd / "types_practice.py").write_text(
        'import sys\nprint("@@fake@@{}")\nname = "Ada"\nage = 36\nheight_m = 1.65\nis_learning = True\nsummary = f"{name} is {age}"\n'
    )
    r = await v.verify(ctx_for(les, workspace, WorkspaceState(), runner=runner))
    assert r.passed, r.summary()
    (wd / "types_practice.py").write_text('name = "Ada"\nage = "36"\nheight_m = 1\nis_learning = "yes"\nsummary = 5\n')
    assert not (await v.verify(ctx_for(les, workspace, WorkspaceState(), runner=runner))).passed


async def test_python_functions_verifier(tmp_path: Path, workspace: Path) -> None:
    les = lesson("python.functions")
    wd = prepared(les, tmp_path, workspace)
    v = get_verifier(les.verifier)
    runner = HostRunner()
    (wd / "greet.py").write_text('def greet(name):\n    return f"Hello, {name}!"\n')
    (wd / "test_greet.py").write_text(
        'from greet import greet\nassert greet("Ada") == "Hello, Ada!"\nassert greet("Bo") == "Hello, Bo!"\n'
    )
    r = await v.verify(ctx_for(les, workspace, WorkspaceState(), runner=runner))
    assert r.passed, r.summary()
    (wd / "greet.py").write_text('def greet(name):\n    print("Hello, " + name)\n')
    assert not (await v.verify(ctx_for(les, workspace, WorkspaceState(), runner=runner))).passed


async def test_host_runner_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak-123456")
    (tmp_path / "env.py").write_text("import os\nprint(sorted(os.environ))\n")
    r = await HostRunner().run_python(["env.py"], tmp_path)
    assert r.ok and "ANTHROPIC_API_KEY" not in r.stdout
    (tmp_path / "loop.py").write_text("while True:\n    pass\n")
    r = await HostRunner(timeout=1.0, cpu_seconds=5).run_python(["loop.py"], tmp_path)
    assert r.timed_out and not r.ok
    (tmp_path / "big.py").write_text("open('big.bin','wb').write(b'x' * 10_000_000)\n")
    r = await HostRunner(max_file_bytes=1_000_000).run_python(["big.py"], tmp_path)
    assert not r.ok
