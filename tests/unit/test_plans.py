from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cli_mentor.curriculum.plans import (
    PLAN_TOOL,
    STEP_TOOL,
    PlanOutline,
    PlanStore,
    StepDetails,
    render_plan,
)
from cli_mentor.curriculum.runner import HostRunner
from cli_mentor.curriculum.setup import apply_setup, cleanup, plan_setup
from cli_mentor.curriculum.verify import VerifyContext, get_verifier
from cli_mentor.db.store import open_store
from cli_mentor.events.schema import EventEnvelope, NvimTechnique, ShellCommandFinished, SourceKind, SourceRef
from cli_mentor.observe.workspace import WorkspaceScope
from cli_mentor.state.projection import WorkspaceState
from tests.plan_helpers import fixture, lesson_for_step

T0 = time.time()


# ---------- schemas ----------
def test_fixture_validates_and_tool_schemas_are_self_contained() -> None:
    data = fixture()
    PlanOutline.model_validate(data["plan"])
    for step in data["steps"]:
        StepDetails.model_validate(step)
    for tool in (PLAN_TOOL, STEP_TOOL):
        assert "$ref" not in str(tool.parameters) and "$defs" not in tool.parameters


@pytest.mark.parametrize(
    "bad",
    [
        {"checks": [{"type": "run_shell", "command": "rm -rf ~"}]},
        {"checks": [{"type": "file_exists", "path": "../../etc/passwd"}]},
        {"checks": [{"type": "program_output", "language": "bash", "source": "x.sh", "expected_stdout": ""}]},
        {"checks": [{"type": "command_succeeded", "program": "rm -rf /"}]},
        {"checks": [{"type": "editor_commands", "commands": [":!rm -rf ~"]}]},
        {"starter_files": [{"path": "/etc/hosts", "content": "x"}]},
        {"hints": []},
        {"extra_field": "nope"},
    ],
)
def test_step_details_reject_unsafe_or_invalid(bad: dict[str, Any]) -> None:
    details = dict(fixture()["steps"][0])
    details.update(bad)
    with pytest.raises(ValidationError):
        StepDetails.model_validate(details)


def test_outline_limits() -> None:
    step = {"title": "Step", "objective": "Learn it"}
    too_big = {"title": "Huge", "modules": [{"title": f"M{i}", "steps": [step] * 12} for i in range(13)]}
    with pytest.raises(ValidationError):
        PlanOutline.model_validate(too_big)


def test_skill_names_are_normalised() -> None:
    details = dict(fixture()["steps"][1])
    details["skills"] = ["C++ Pointers!", "  Memory  "]
    assert StepDetails.model_validate(details).skills == ["cpp_pointers", "memory"]


# ---------- store ----------
def test_plan_lifecycle(tmp_path: Path) -> None:
    store = open_store(tmp_path / "db.sqlite3")
    plans = PlanStore(store)
    outline = PlanOutline.model_validate(fixture()["plan"])
    plan = plans.create("I want to learn C++", outline, "mock", "m")
    assert plan.status == "proposed" and plans.active() is None
    assert [s.step_id for s in plans.steps(plan.id)] == ["s1", "s2", "s3"]
    revised = outline.model_copy(update={"title": "C++ revised"})
    plan = plans.revise(plan.id, revised)
    assert plan.title == "C++ revised"
    plan = plans.activate(plan.id)
    assert plan.status == "active" and plan.current_step == "s1"
    nxt = plans.finish_step(plan.id, "s1")
    assert nxt is not None and nxt.step_id == "s2"
    assert plans.progress(plan.id) == (1, 3)
    plans.finish_step(plan.id, "s2", "skipped")
    assert plans.finish_step(plan.id, "s3") is None
    final = plans.get(plan.id)
    assert final is not None and final.status == "completed" and final.current_step is None
    # A new active plan archives the old active one.
    other = plans.activate(plans.create("Rust", outline).id)
    assert plans.active() == other


def test_build_lesson_derives_risk_and_paths(tmp_path: Path) -> None:
    store = open_store(tmp_path / "db.sqlite3")
    lesson = lesson_for_step(store, 0)
    assert lesson.id.startswith("plan.p") and lesson.id.endswith(".s1")
    assert lesson.risk == "runs_learner_code"  # decided by trusted code from the checks
    assert lesson.workdir.startswith("plans/c-from-the-very-basics-")
    assert lesson.hint(1) is not None and lesson.hint(3) is not None
    assert "`<<` — send" in lesson.explanation_markdown()
    assert PlanStore(store).lesson(lesson.id) == lesson
    assert lesson_for_step(open_store(tmp_path / "db2.sqlite3"), 1).risk == "writes_workspace"


def test_render_plan_marks_progress(tmp_path: Path) -> None:
    store = open_store(tmp_path / "db.sqlite3")
    plans = PlanStore(store)
    plan = plans.activate(plans.create("x", PlanOutline.model_validate(fixture()["plan"])).id)
    plans.finish_step(plan.id, "s1")
    text = render_plan(plan.outline, plans.steps(plan.id), "s2")
    assert "[done] 1. Hello, C++" in text and "[now] 2. Variables" in text and "[ ] 3." in text
    assert "**Module 2: Thinking like a programmer**" in text


# ---------- setup / cleanup ----------
def test_setup_and_cleanup_of_generated_step(tmp_path: Path, workspace: Path) -> None:
    store = open_store(tmp_path / "db.sqlite3")
    lesson = lesson_for_step(store, 0)
    scope = WorkspaceScope([workspace])
    apply_setup(plan_setup(lesson, workspace, scope), store)
    starter = workspace / lesson.workdir / "hello.cpp"
    assert starter.read_text().startswith("// Write your first")
    (workspace / lesson.workdir / "mine.txt").write_text("my notes")
    report = dict(cleanup(lesson, workspace, store))
    assert report[str(starter)] == "removed"
    assert (workspace / lesson.workdir / "mine.txt").exists()


# ---------- the trusted generic checker ----------
def ctx(lesson: Any, workspace: Path, state: WorkspaceState, **kw: Any) -> VerifyContext:
    return VerifyContext(
        workdir=workspace / lesson.workdir,
        scope=WorkspaceScope([workspace]),
        state=state,
        params=lesson.verifier_params,
        attempt_started_at=T0,
        exercise_id=lesson.id,
        **kw,
    )


def shell(cmd: str, cwd: Path, status: int = 0, seq: int = 1) -> EventEnvelope:
    return EventEnvelope(
        session_id="s",
        source=SourceRef(kind=SourceKind.SHELL, id="z", epoch="e"),
        seq=seq,
        captured_at=T0 + seq,
        received_at=T0 + seq,
        payload=ShellCommandFinished(
            command=cmd, cwd=str(cwd), exit_status=status, started_at=T0 + seq, finished_at=T0 + seq + 0.1
        ),
    )


async def test_cpp_compile_and_run_check(tmp_path: Path, workspace: Path) -> None:
    if shutil.which("c++") is None:
        pytest.skip("no C++ compiler")
    store = open_store(tmp_path / "db.sqlite3")
    lesson = lesson_for_step(store, 0)
    apply_setup(plan_setup(lesson, workspace, WorkspaceScope([workspace])), store)
    wd = workspace / lesson.workdir
    st = WorkspaceState()
    v = get_verifier(lesson.verifier)
    # Not allowed to run code: the program check cannot pass.
    r = await v.verify(ctx(lesson, workspace, st))
    assert not r.passed
    # Compile error is reported from the real compiler.
    (wd / "hello.cpp").write_text("int main() { std::cout << 1 }\n")
    r = await v.verify(ctx(lesson, workspace, st, runner=HostRunner()))
    prog = next(c for c in r.checks if "expected output" in c.name)
    assert not prog.passed and "did not compile" in prog.detail and "error" in prog.detail
    # Correct program and an observed successful compile -> pass.
    (wd / "hello.cpp").write_text('#include <iostream>\nint main() { std::cout << "Hello, C++!" << std::endl; }\n')
    st.apply(shell("c++ hello.cpp -o hello", wd))
    r = await v.verify(ctx(lesson, workspace, st, runner=HostRunner()))
    assert r.passed and r.executed_learner_code, r.summary()


async def test_answer_review_and_editor_checks(tmp_path: Path, workspace: Path) -> None:
    store = open_store(tmp_path / "db.sqlite3")
    lesson = lesson_for_step(store, 1)
    v = get_verifier(lesson.verifier)
    assert not (await v.verify(ctx(lesson, workspace, WorkspaceState(), answer="float"))).passed
    assert (await v.verify(ctx(lesson, workspace, WorkspaceState(), answer=" INT. "))).passed
    review = lesson_for_step(open_store(tmp_path / "db2.sqlite3"), 2)
    r = await v.verify(ctx(review, workspace, WorkspaceState()))
    assert r.needs_review and not r.passed and "No automatic check" in r.summary()
    edit = lesson_for_step(
        open_store(tmp_path / "db3.sqlite3"),
        1,
        checks=[
            {"type": "file_contains", "path": "a.txt", "text": "the"},
            {"type": "editor_commands", "commands": ["ciw"]},
        ],
    )
    (workspace / edit.workdir).mkdir(parents=True)
    (workspace / edit.workdir / "a.txt").write_text("I like the color\n")
    st = WorkspaceState()
    r = await v.verify(ctx(edit, workspace, st, telemetry_enabled=False))
    assert r.passed and r.technique == "unverified"
    st.apply(
        EventEnvelope(
            session_id="s",
            source=SourceRef(kind=SourceKind.NVIM, id="n", epoch="e"),
            seq=1,
            captured_at=T0,
            received_at=T0,
            payload=NvimTechnique(exercise_id=edit.id, buf=1, counts={"ciw": 1}),
        )
    )
    assert (await v.verify(ctx(edit, workspace, st, telemetry_enabled=True))).technique == "verified"


async def test_command_check_skips_env_prefix_and_needs_success(tmp_path: Path, workspace: Path) -> None:
    lesson = lesson_for_step(
        open_store(tmp_path / "db.sqlite3"), 1, checks=[{"type": "command_succeeded", "program": "make"}]
    )
    v = get_verifier(lesson.verifier)
    st = WorkspaceState()
    st.apply(shell("make all", workspace, status=2, seq=1))
    r = await v.verify(ctx(lesson, workspace, st))
    assert not r.passed and "exit 2" in r.checks[0].detail
    st.apply(shell("CC=clang /usr/bin/make all", workspace, seq=2))
    assert (await v.verify(ctx(lesson, workspace, st))).passed


# ---------- host runner limits ----------
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
    assert os.environ.get("ANTHROPIC_API_KEY")  # only removed for the child
