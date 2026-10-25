"""Conversation regressions, using recorded model judgments and isolated learner data.

These verify how judgments affect the application, not a real model's language ability.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from cli_mentor.events.schema import Provenance, TmuxOutput, TmuxSnapshot
from cli_mentor.providers.base import Done, ProviderRequest, StreamEvent, ToolCall
from cli_mentor.tutor import commands
from cli_mentor.tutor.ui_protocol import HeadlessUi
from tests.engine_helpers import FakeShell, make_engine, settle
from tests.plan_helpers import planning_mock
from tests.unit.test_engine import HELLO, reach_first_step
from tests.unit.test_real_model_regressions import Scripted, silent


@pytest.fixture
def ws(workspace: Path, mentor_env: dict[str, str]) -> Path:
    return workspace


@pytest.mark.parametrize("answer", ["the DBMS", "I think the database engine", "DBMS."])
async def test_ordinary_wording_uses_semantic_judgment_and_continues(ws: Path, answer: str) -> None:
    mock = planning_mock(
        reply={
            "verdict": "understood",
            "feedback": "Yes—the DBMS manages the data.",
            "independent": False,
        }
    )
    mock.behaviour.steps[0]["checks"] = [
        {
            "type": "learner_answer",
            "question": "What manages the stored data?",
            "accepted": ["DBMS"],
        }
    ]
    ui = HeadlessUi()
    eng = make_engine(ws, provider=mock, ui=ui)
    await eng.start(greet=False)
    try:
        await reach_first_step(eng)
        await eng.skip_prediction()
        lesson = eng.lesson()
        assert lesson is not None
        await eng.learner_message(answer)
        interpretation = next(r for r in mock.requests if r.meta and r.meta.learner_text == answer)
        assert [t.name for t in interpretation.tools or []] == ["interpret_reply"]
        assert answer in interpretation.messages[-1].content
        assert "Reference meanings" in interpretation.messages[-1].content
        # No canonical answer is invented and passed to the trusted checker.
        assert not eng.store.conn.execute("SELECT 1 FROM skill_evidence WHERE kind = 'verification'").fetchall()
        rows = eng.store.conn.execute(
            "SELECT result, provenance, details FROM skill_evidence WHERE exercise_id = ?", (lesson.id,)
        ).fetchall()
        assert rows and all(r[0] == "unverified" and r[1] == "model_inferred" for r in rows)
        assert all(json.loads(r[2])["answer"] == answer for r in rows)
        assert all(s.independent_passes == 0 for s in eng.evidence.skill_states())
        assert eng.store.conn.execute("SELECT DISTINCT assistance_level FROM skill_evidence").fetchall()[0][0] == 0
        assert eng.lesson() is not None and eng.lesson().title == "Variables and types"  # type: ignore[union-attr]
        assert eng.session.exercise_stage == "explain"  # next idea, without a request to say next
        assert not any("Say **next**" in t.text for t in ui.turns.values() if t.trigger == "learner_message")
    finally:
        await eng.stop()


@pytest.mark.parametrize("message", ["explain what include means", "Before I predict: explain the brackets"])
async def test_clarification_without_question_mark_keeps_question_and_attempt(ws: Path, message: str) -> None:
    mock = planning_mock(
        reply={
            "verdict": "clarification",
            "feedback": "The brackets surround the header name.",
        }
    )
    ui = HeadlessUi()
    eng = make_engine(ws, provider=mock, ui=ui)
    await eng.start(greet=False)
    try:
        await reach_first_step(eng)
        lesson = eng.lesson()
        assert lesson is not None
        pending = list(eng.session.open_questions)
        attempt = eng.evidence.current_attempt(lesson.id)
        await eng.learner_message(message)
        assert eng.session.exercise_stage == "explain" and eng.session.open_questions == pending
        assert eng.evidence.current_attempt(lesson.id) == attempt
        assert not eng.store.conn.execute("SELECT 1 FROM skill_evidence").fetchall()
        reply = ui.last_turn("prediction")
        assert reply is not None and "header name" in reply.text and pending[0] in reply.text
    finally:
        await eng.stop()


@pytest.mark.parametrize("message", ["", "  ", "?!...?!"])
async def test_missing_or_scrambled_answer_never_becomes_negative_evidence(ws: Path, message: str) -> None:
    ui = HeadlessUi()
    eng = make_engine(ws, provider=planning_mock(), ui=ui)
    await eng.start(greet=False)
    try:
        await reach_first_step(eng)
        pending = list(eng.session.open_questions)
        await eng.learner_message(message)
        assert eng.session.exercise_stage == "explain" and eng.session.open_questions == pending
        assert not eng.store.conn.execute("SELECT 1 FROM skill_evidence").fetchall()
        reply = ui.last_turn("learner_message")
        assert reply is not None and "didn't receive an answer" in reply.text
    finally:
        await eng.stop()


async def test_wrong_answer_gets_smaller_question_and_keeps_assistance(ws: Path) -> None:
    mock = planning_mock(
        reply={
            "verdict": "needs_work",
            "feedback": "A compiler translates code before it runs.",
            "follow_up": "Does compilation happen before or after running?",
        }
    )
    ui = HeadlessUi()
    eng = make_engine(ws, provider=mock, ui=ui)
    await eng.start(greet=False)
    try:
        await reach_first_step(eng)
        lesson = eng.lesson()
        assert lesson is not None
        original = list(eng.session.open_questions)
        await eng.learner_message("It translates after running")
        assert eng.session.exercise_stage == "explain"
        assert eng.session.open_questions != original
        reply = ui.last_turn("prediction")
        assert reply is not None and "before or after" in reply.text and "fail" not in reply.text.lower()
        assert not eng.store.conn.execute("SELECT 1 FROM skill_evidence WHERE result = 'fail'").fetchall()
        attempt = eng.evidence.current_attempt(lesson.id)
        assert attempt is not None and attempt.max_assistance == 1
        sid = eng.session_id
    finally:
        await eng.stop()
    resumed = make_engine(ws, provider=planning_mock(), session_id=sid)
    await resumed.start(greet=False)
    try:
        assert resumed.session.open_questions == ["Does compilation happen before or after running?"]
        assert resumed.session.exercise_stage == "explain"
    finally:
        await resumed.stop()


@pytest.mark.parametrize("source", ["shell", "output", "snapshot"])
async def test_shell_mixup_gives_one_line_without_consuming_answer(ws: Path, source: str) -> None:
    ui = HeadlessUi()
    mock = planning_mock()
    eng = make_engine(ws, provider=mock, ui=ui)
    await eng.start(greet=False)
    try:
        wd = await reach_first_step(eng)
        pending = list(eng.session.open_questions)
        calls_before = len(mock.requests)
        if source == "shell":
            FakeShell(eng).run("/check", wd, status=127)
        elif source == "output":
            eng._on_terminal(
                "c1", 1, TmuxOutput(pane_id="%2", text="zsh: command not found: /check\r\n"), Provenance.OBSERVED
            )
        else:
            eng._on_terminal(
                "c1",
                1,
                TmuxSnapshot(pane_id="%2", lines=["learner@mac ws %", "learner@mac ws %"], reason="baseline"),
                Provenance.SNAPSHOT,
            )
        await settle(eng)
        turn = ui.last_turn("explain")
        assert turn is not None and turn.text == "Type your reply in the tutor pane on the left."
        assert eng.session.open_questions == pending and eng.session.exercise_stage == "explain"
        assert len(mock.requests) == calls_before
        assert not eng.store.conn.execute("SELECT 1 FROM skill_evidence").fetchall()
        assert eng.policy.consecutive_failures == 0
    finally:
        await eng.stop()


@pytest.mark.skipif(shutil.which("c++") is None, reason="no C++ compiler")
async def test_successful_execution_waits_for_understanding_then_continues(ws: Path) -> None:
    mock = planning_mock()
    ui = HeadlessUi()
    eng = make_engine(ws, provider=mock, ui=ui)
    await eng.start(greet=False)
    try:
        wd = await reach_first_step(eng)
        await eng.skip_prediction()
        lesson = eng.lesson()
        assert lesson is not None
        (wd / "hello.cpp").write_text(HELLO)
        FakeShell(eng).run("c++ hello.cpp -o hello", wd)
        await settle(eng)
        result = await eng.check()
        assert result is not None and result.passed
        assert eng.session.exercise_stage == "reflect"
        assert eng.status_info().step == 1 and eng.status_info().step_stage == "reflect"
        assert all(s.independent_passes == 0 for s in eng.evidence.skill_states())
        transcript = "\n".join(t.text for t in ui.turns.values())
        assert "[PASS]" not in transcript and "[FAIL]" not in transcript and "Check result" not in transcript
        mock.behaviour.reply = {
            "verdict": "understood",
            "feedback": "Yes—each newline starts another line.",
            "independent": True,
        }
        await eng.learner_message("I would send each message to cout followed by a newline")
        old_states = [s for s in eng.evidence.skill_states() if s.skill in lesson.skills]
        assert all(s.independent_passes == 1 for s in old_states)
        evidence = eng.store.conn.execute(
            "SELECT result, provenance FROM skill_evidence WHERE kind = 'transfer'"
        ).fetchall()
        assert evidence and all(tuple(r) == ("unverified", "model_inferred") for r in evidence)
        assert eng.lesson() is not None and eng.lesson().title == "Variables and types"  # type: ignore[union-attr]
    finally:
        await eng.stop()


async def test_mixed_task_keeps_semantics_separate_from_trusted_file_check(ws: Path) -> None:
    mock = planning_mock(reply={"verdict": "understood", "feedback": "Yes—the DBMS manages the data."})
    mock.behaviour.steps[0]["checks"] = [
        {"type": "learner_answer", "question": "What manages the data?", "accepted": ["DBMS"]},
        {"type": "file_exists", "path": "notes.txt"},
    ]
    eng = make_engine(ws, provider=mock)
    await eng.start(greet=False)
    try:
        wd = await reach_first_step(eng)
        await eng.skip_prediction()
        (wd / "notes.txt").write_text("my notes")
        await eng.learner_message("the database engine")
        assert eng.session.exercise_stage == "reflect"
        rows = eng.store.conn.execute("SELECT details FROM skill_evidence WHERE kind = 'verification'").fetchall()
        assert rows
        for r in rows:
            checks = json.loads(r[0])["checks"]
            assert len(checks) == 1 and checks[0]["evidence"] == "filesystem"
        assert eng.store.conn.execute("SELECT 1 FROM skill_evidence WHERE provenance = 'model_inferred'").fetchone()
    finally:
        await eng.stop()


async def test_prediction_that_answers_the_task_does_not_require_repetition(ws: Path) -> None:
    mock = planning_mock(
        reply={
            "verdict": "understood",
            "feedback": "Yes—the DBMS manages the data.",
            "answers_task": True,
        }
    )
    mock.behaviour.steps[0]["checks"] = [
        {"type": "learner_answer", "question": "What manages the data?", "accepted": ["DBMS"]},
    ]
    eng = make_engine(ws, provider=mock)
    await eng.start(greet=False)
    try:
        await reach_first_step(eng)
        await eng.learner_message("the database engine")
        assert eng.lesson() is not None and eng.lesson().title == "Variables and types"  # type: ignore[union-attr]
        assert (
            eng.store.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE role = 'learner' AND content = 'the database engine'"
            ).fetchone()[0]
            == 1
        )
    finally:
        await eng.stop()


async def test_broken_tool_reply_retries_saved_answer_without_penalizing_learner(ws: Path) -> None:
    correct = ToolCall("interpret_reply", {"verdict": "understood", "feedback": "Yes, that is what it prints."})
    mock = Scripted(prediction=[[Done("end_turn")], silent(correct)])
    eng = make_engine(ws, provider=mock)
    await eng.start(greet=False)
    try:
        await reach_first_step(eng)
        await eng.learner_message("It prints a greeting")
        replies = [r for r in mock.requests if r.meta and r.meta.trigger == "prediction"]
        assert len(replies) == 2
        assert all(r.meta and r.meta.learner_text == "It prints a greeting" for r in replies)
        assert eng.session.exercise_stage == "attempt"
        assert (
            eng.store.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE role = 'learner' AND content = 'It prints a greeting'"
            ).fetchone()[0]
            == 1
        )
        assert not eng.store.conn.execute("SELECT 1 FROM skill_evidence WHERE result = 'fail'").fetchall()
    finally:
        await eng.stop()


async def test_old_shell_error_does_not_retrigger_reminder_after_a_reply(ws: Path) -> None:
    ui = HeadlessUi()
    eng = make_engine(ws, provider=planning_mock(), ui=ui)
    await eng.start(greet=False)
    try:
        await reach_first_step(eng)
        eng._on_terminal(
            "c1", 1, TmuxOutput(pane_id="%2", text="zsh: command not found: /check\r\n"), Provenance.OBSERVED
        )
        await settle(eng)
        await eng.learner_message("explain what include means")
        before = sum(t.text == "Type your reply in the tutor pane on the left." for t in ui.turns.values())
        eng._on_terminal("c1", 2, TmuxOutput(pane_id="%2", text="normal output\r\nlearner % "), Provenance.OBSERVED)
        await settle(eng)
        assert sum(t.text == "Type your reply in the tutor pane on the left." for t in ui.turns.values()) == before
        await commands.dispatch(eng, "/progress")
        progress = ui.notes[-1][1]
        assert "Recent attempts" not in progress and "not yet" not in progress
    finally:
        await eng.stop()


async def test_terminal_refresh_does_not_discard_a_reply_to_the_same_question(ws: Path) -> None:
    class Refreshing(Scripted):
        async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamEvent]:
            if request.meta and request.meta.trigger == "prediction":
                FakeShell(eng).run("pwd", ws)
                await settle(eng)
            async for event in super().stream(request):
                yield event

    mock = Refreshing()
    eng = make_engine(ws, provider=mock)
    await eng.start(greet=False)
    try:
        await reach_first_step(eng)
        await eng.learner_message("It prints a greeting")
        assert eng.inflight is not None and eng.inflight.result_status == "stale"
        assert eng.session.exercise_stage == "attempt"
        assert len([r for r in mock.requests if r.meta and r.meta.trigger == "prediction"]) == 1
        assert not eng.store.conn.execute("SELECT 1 FROM skill_evidence WHERE result = 'fail'").fetchall()
    finally:
        await eng.stop()


async def test_eager_ui_task_completion_does_not_lose_the_interpretation(ws: Path) -> None:
    loop = asyncio.get_running_loop()
    previous = loop.get_task_factory()
    loop.set_task_factory(asyncio.eager_task_factory)
    eng = make_engine(ws, provider=planning_mock())
    try:
        await eng.start(greet=False)
        await reach_first_step(eng)
        await eng.learner_message("It prints a greeting")
        assert eng.session.exercise_stage == "attempt"
        assert eng.inflight is not None and eng.inflight.result_status == "complete"
    finally:
        await eng.stop()
        loop.set_task_factory(previous)
