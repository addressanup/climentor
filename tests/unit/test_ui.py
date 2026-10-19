"""Textual UI tests using the Pilot test driver (no real terminal needed)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cli_mentor.providers.mock import MockProvider
from cli_mentor.tutor.engine import Engine
from cli_mentor.ui.app import ChoiceScreen, Entry, MentorApp, PromptInput
from tests.engine_helpers import make_engine
from tests.plan_helpers import planning_mock


@pytest.fixture
def ws(workspace: Path, mentor_env: dict[str, str]) -> Path:
    return workspace


def texts(app: MentorApp) -> str:
    return "\n".join(entry.plain() for entry in app.query(Entry))


def line(app: MentorApp, wid: str) -> str:
    return str(app.query_one(wid).render())


async def answer_modal(pilot, key: str) -> None:  # type: ignore[no-untyped-def]
    for _ in range(50):
        if isinstance(pilot.app.screen, ChoiceScreen):
            await pilot.press(key)
            return
        await pilot.pause(0.05)
    raise AssertionError("no question dialog appeared")


async def test_ui_starts_streams_and_shows_status(ws: Path) -> None:
    provider = MockProvider()
    app = MentorApp(lambda ui: make_engine(ws, ui=ui, provider=provider))  # type: ignore[arg-type]
    async with app.run_test(size=(100, 40)) as pilot:
        await answer_modal(pilot, "s")  # allow observation for this session
        await pilot.pause(0.3)
        status = line(app, "#state")
        assert "● observing" in status and "offline mock" in status and "mode" not in status
        assert "New subject" in line(app, "#subject")
        assert "Welcome to CLI Mentor" in texts(app)  # first session: the short tour
        assert "What would you like to learn?" in texts(app)
        assert "What do you want to learn?" in str(app.query_one("#input", PromptInput).placeholder)
        await pilot.click("#input")
        for ch in "hello tutor":
            await pilot.press(ch if ch != " " else "space")
        await pilot.press("enter")
        await pilot.pause(0.5)
        learner = [e for e in app.query(Entry) if e.kind == "learner"]
        assert learner and learner[-1].text == "hello tutor"
        assert "needs a real AI model" in texts(app)  # the plain mock cannot design a plan
        assert app.query_one("#input").text == ""  # type: ignore[attr-defined]


async def test_ui_multiline_and_slash_commands(ws: Path) -> None:
    app = MentorApp(lambda ui: make_engine(ws, ui=ui))  # type: ignore[arg-type]
    async with app.run_test(size=(100, 40)) as pilot:
        await answer_modal(pilot, "s")
        await pilot.pause(0.2)
        await pilot.press("a", "ctrl+j", "b")
        assert app.query_one("#input").text == "a\nb"  # type: ignore[attr-defined]
        app.query_one("#input").text = "/help"  # type: ignore[attr-defined]
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert "/observe pause" in texts(app)


async def test_ui_lesson_approval_and_cancel(ws: Path) -> None:
    slow = planning_mock()
    engines: list[Engine] = []

    def factory(ui: MentorApp) -> Engine:
        eng = make_engine(ws, ui=ui, provider=slow)
        engines.append(eng)
        return eng

    app = MentorApp(factory)  # type: ignore[arg-type]
    async with app.run_test(size=(100, 40)) as pilot:
        await answer_modal(pilot, "s")
        await pilot.pause(0.2)
        app.query_one("#input").text = "I want to learn C++"  # type: ignore[attr-defined]
        await pilot.press("enter")
        await pilot.pause(0.4)
        assert "Module 1: First programs" in texts(app)
        assert "proposed plan · 3 steps" in line(app, "#where")
        assert "Type yes to start" in str(app.query_one("#input", PromptInput).placeholder)
        app.query_one("#input").text = "yes"  # type: ignore[attr-defined]
        await pilot.press("enter")
        await answer_modal(pilot, "y")  # create the step's files: yes, this time
        await pilot.pause(0.5)
        assert list(ws.glob("*/01-*/hello.cpp"))
        assert "New words" in texts(app)
        assert line(app, "#subject") == "C++ from the very basics" and line(app, "#where") == "step 1 of 3"
        assert "Hello, C++" in line(app, "#step") and "● learn" in line(app, "#stages")
        assert "prediction" in str(app.query_one("#input", PromptInput).placeholder)
        slow.behaviour.chunk_delay = 0.05
        app.query_one("#input").text = "/explain"  # type: ignore[attr-defined]
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert "writing" in line(app, "#state") and "Esc stop the reply" in line(app, "#keys")
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert "— cancelled — stopped by you" in texts(app)
        assert engines[0].inflight is not None and engines[0].inflight.task.done()


async def test_ui_quit_end_session_warns_about_unsaved(ws: Path) -> None:
    engines: list[Engine] = []

    def factory(ui: MentorApp) -> Engine:
        eng = make_engine(ws, ui=ui)
        engines.append(eng)
        return eng

    app = MentorApp(factory)  # type: ignore[arg-type]
    async with app.run_test(size=(100, 40)) as pilot:
        await answer_modal(pilot, "s")
        await pilot.pause(0.2)
        from cli_mentor.state.projection import BufferMirror

        engines[0].state.buffers[("nvim-1", 1)] = BufferMirror("nvim-1", 1, str(ws / "a.txt"), modified=True)
        await pilot.press("ctrl+q")
        await answer_modal(pilot, "e")
        await pilot.pause(0.1)
        assert isinstance(app.screen, ChoiceScreen) and "Unsaved" in app.screen.title_text
        await pilot.press("n")  # go back: nothing is closed
        await pilot.pause(0.1)
        assert app.is_running
    await asyncio.sleep(0)
