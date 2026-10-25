"""Regressions found by running a real model (DeepSeek) through the whole learning flow.

Each test replays what the real model did, with the offline mock and scripted replies.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from cli_mentor.curriculum.adapters.generic import answer_matches
from cli_mentor.curriculum.plans import LearnerAnswer, ModuleOutline, StepOutline, _prose, slug
from cli_mentor.permissions import Capability
from cli_mentor.providers.base import Done, ProviderRequest, StreamEvent, TextDelta, ToolCall, Usage
from cli_mentor.providers.mock import MockBehaviour, MockProvider
from cli_mentor.tutor.engine import MIN_REPLY_TOKENS
from cli_mentor.tutor.ui_protocol import HeadlessUi
from tests.engine_helpers import FakeNvim, make_engine, settle
from tests.plan_helpers import FIXTURE
from tests.unit.test_engine import reach_first_step


@pytest.fixture
def ws(workspace: Path, mentor_env: dict[str, str]) -> Path:
    return workspace


class Scripted(MockProvider):
    """The recorded-plan mock, plus scripted replies for chosen triggers (used once each, in order)."""

    def __init__(self, **scripts: list[list[StreamEvent]]) -> None:
        super().__init__(behaviour=MockBehaviour.from_fixture(FIXTURE))
        self.scripts = scripts

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamEvent]:
        queue = self.scripts.get(request.meta.trigger if request.meta else "")
        if queue:
            self.requests.append(request)
            for event in queue.pop(0):
                yield event
            return
        async for event in super().stream(request):
            yield event


def silent(*calls: ToolCall) -> list[StreamEvent]:
    """A reply with tool calls and no words, as DeepSeek often sent."""
    return [*calls, Usage(10, 1, True), Done("tool_calls")]


# ---------------------------------------------------------------- pure functions
def test_answers_in_a_sentence_pass_but_hedges_and_negations_do_not() -> None:
    accepted = ["clang", "apple clang"]
    assert answer_matches("clang", accepted)
    assert answer_matches("the first line says Apple clang version 21 so c++ is actually clang", accepted)
    assert not answer_matches("gcc or clang", accepted)
    assert not answer_matches("it is not clang", accepted)
    assert not answer_matches("it isn't clang", accepted)
    assert not answer_matches("", accepted)
    assert not answer_matches("it prints 14", ["4"])


def test_model_text_is_cleaned_up() -> None:
    step = StepOutline(title="Hello", objective="Say hello")
    assert ModuleOutline(title="Module 3: Loops", steps=[step]).title == "Loops"
    assert ModuleOutline(title="Part 2 - Types", steps=[step]).title == "Types"
    assert slug("C++ From First Program to Advanced Features", 40) == "c-from-first-program-to-advanced"
    assert _prose("1. Click the shell.\\n2. Type it.") == "1. Click the shell.\n2. Type it."
    assert _prose("Real line\nwith \\n inside code") == "Real line\nwith \\n inside code"
    with pytest.raises(ValueError, match="short facts"):
        LearnerAnswer(type="learner_answer", question="Why?", accepted=["translates the whole program first"])


# ---------------------------------------------------------------- engine
async def test_rejected_step_is_retried_with_the_reason(ws: Path) -> None:
    bad = ToolCall("prepare_step", {"skills": ["x"], "instructions": "short", "hints": []})
    mock = Scripted(prepare_step=[silent(bad)])
    eng = make_engine(ws, provider=mock)
    await eng.start(greet=False)
    try:
        await reach_first_step(eng)  # the second, valid prepare_step comes from the recording
        retry = [r for r in mock.requests if r.meta and r.meta.trigger == "prepare_step"][-1]
        assert "was rejected" in retry.messages[-1].content and "instructions" in retry.messages[-1].content
    finally:
        await eng.stop()


async def test_step_folder_exists_without_starter_files(ws: Path) -> None:
    eng = make_engine(ws, provider=Scripted())
    await eng.start(greet=False)
    try:
        await reach_first_step(eng)
        await eng.skip_step()
        await eng.start_step()  # step 2 has no starter files
        wd = eng.exercise_dir()
        assert wd is not None and wd.is_dir()
    finally:
        await eng.stop()


async def test_between_steps_a_silent_tool_call_still_gets_an_answer(ws: Path) -> None:
    ui = HeadlessUi()
    mock = Scripted(learner_message=[silent(ToolCall("request_check", {"answer": "x"}))])
    eng = make_engine(ws, ui=ui, provider=mock)
    await eng.start(greet=False)
    try:
        await reach_first_step(eng)
        await eng.skip_step()  # between steps now
        await eng.learner_message("I think hello.cpp is my code and -o names the program file it makes")
        asks = [r for r in mock.requests if r.meta and r.meta.trigger == "learner_message"]
        assert [t.name for t in asks[0].tools] == ["propose_memory", "start_step"]  # nothing to check
        assert len(asks) == 2 and asks[1].tools == []  # the follow-up asks for words only
        reply = ui.last_turn("learner_message")
        assert reply is not None and reply.text.strip()
        assert not ui.statuses[-1].busy  # the spinner stops once the reply is done
    finally:
        await eng.stop()


async def test_a_question_before_predicting_keeps_the_prediction_open(ws: Path) -> None:
    ui = HeadlessUi()
    eng = make_engine(ws, ui=ui, provider=Scripted())
    await eng.start(greet=False)
    try:
        await reach_first_step(eng)
        await eng.learner_message("what does #include mean?")
        assert eng.session.exercise_stage == "explain"
        assert ui.last_turn("prediction") is not None
    finally:
        await eng.stop()


async def test_run_code_for_the_session_covers_later_steps(ws: Path) -> None:
    from tests.unit.test_engine import HELLO

    ui = HeadlessUi(answers={"Run your code": "s"})
    eng = make_engine(ws, ui=ui, provider=Scripted())
    await eng.start(greet=False)
    try:
        wd = await reach_first_step(eng)
        await eng.skip_prediction()
        (wd / "hello.cpp").write_text(HELLO)
        await eng.check()
        later = wd.parent / "02-variables-and-types"
        assert eng.permissions.check(Capability.RUN_LEARNER_CODE, later).allowed
    finally:
        await eng.stop()


async def test_cut_off_replies_are_labelled_and_replies_get_room(ws: Path) -> None:
    ui = HeadlessUi()
    mock = Scripted(hint=[[TextDelta("Look at line"), Usage(10, 1024, False), Done("length")]])
    eng = make_engine(ws, ui=ui, provider=mock)
    await eng.start(greet=False)
    try:
        await reach_first_step(eng)
        await eng.skip_prediction()
        await eng.hint()
        turn = ui.last_turn("hint")
        assert turn is not None and turn.status == "cut_off" and "ran out of room" in turn.note
        assert all(r.max_output_tokens >= MIN_REPLY_TOKENS for r in mock.requests)
        await eng.learner_message("please finish that hint you were giving me")
        history = [m.content for m in mock.requests[-1].messages]
        assert "Look at line" in history  # the model sees what it already wrote, so it can finish it
    finally:
        await eng.stop()


async def test_neovim_status_recovers_after_a_gap_and_a_second_editor_closing(ws: Path) -> None:
    eng = make_engine(ws, provider=Scripted())
    await eng.start(greet=False)
    try:
        main, extra = FakeNvim(eng, "nvim-1"), FakeNvim(eng, "nvim-2")

        def cursor(nvim: FakeNvim, seq: int) -> None:
            payload: dict[str, Any] = {"type": "nvim.cursor", "buf": 1, "line": 0, "col": 0}
            nvim.seq = seq - 1
            nvim.send(payload)

        cursor(main, 1)
        cursor(extra, 1)
        cursor(main, 5)  # a gap, as after a tutor restart
        await settle(eng)
        assert eng.state.sources[("nvim", "nvim-1")].status.value == "incomplete"
        cursor(main, 6)
        await settle(eng)
        assert eng.state.sources[("nvim", "nvim-1")].status.value == "current"
        eng._on_ipc_connect(extra.ident, False)  # the extra editor (opened in the shell) closes
        assert ("nvim", "nvim-2") not in eng.state.sources
        assert eng.state.sources[("nvim", "nvim-1")].status.value == "current"
    finally:
        await eng.stop()


# ---------------------------------------------------------------- tutor pane
async def test_pane_follows_new_messages_and_hides_empty_ones(ws: Path) -> None:
    from cli_mentor.ui.app import Entry, MentorApp

    app = MentorApp(lambda ui: make_engine(ws, ui=ui))  # type: ignore[arg-type]
    async with app.run_test(size=(75, 30)) as pilot:
        await pilot.pause(0.3)
        log = app._log_view()
        before = len(log.query(Entry))
        app.tutor_begin(901, "learner_message", "deepseek")
        app.tutor_end(901, "complete")  # only a tool call, no words
        await pilot.pause(0.2)
        assert len(log.query(Entry)) == before  # no empty “(no text)” bubble
        app.tutor_begin(902, "plan", "CLI Mentor")
        app.tutor_delta(902, "\n".join(f"{n}. step {n}" for n in range(1, 120)))  # a long plan, right after
        app.tutor_end(902, "complete")
        await pilot.pause(1.0)
        assert log.scroll_y == log.max_scroll_y > 0  # the plan is in view, not stuck above it
        await pilot.press("pageup")  # works even though the text box has the focus
        await pilot.pause(0.2)
        held = log.scroll_y
        assert held < log.max_scroll_y
        app.tutor_begin(903, "explain", "deepseek")
        app.tutor_delta(903, "more\n" * 10)
        app.tutor_end(903, "complete")
        await pilot.pause(0.5)
        assert log.scroll_y == held  # reading further up is not interrupted
