"""Textual UI tests using the Pilot test driver (no real terminal needed)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Markdown

from cli_mentor.providers.mock import MockProvider
from cli_mentor.tutor.engine import Engine
from cli_mentor.ui.app import ChoiceScreen, Entry, MentorApp, PromptInput
from cli_mentor.ui.widgets import Line
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
        assert "Say yes to start" in str(app.query_one("#input", PromptInput).placeholder)
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


class Idle:
    """An engine that does nothing, so a test drives the pane by hand."""

    async def start(self) -> None:
        pass


REPLY = """Good: words first.

### One tiny table

```
 name  | born
-------+------
 Grace | 1906
```

- **data**: the facts. `Grace` is a fact.
- **database**: an organised container of data.

| word | means |
|------|-------|
| SQL  | the language |

That is what **declarative** means: you state *what*, the machine works out *how*.
"""


async def test_ui_streams_a_long_reply_without_falling_behind() -> None:
    """Redrawing the whole reply on every update queued up, so long replies fell behind and arrived late."""
    reply = REPLY * 4
    app = MentorApp(lambda ui: Idle())  # type: ignore[arg-type,return-value]
    async with app.run_test(size=(80, 30)) as pilot:
        app.tutor_begin(1, "lesson_intro", "openai · deepseek-flash")
        md = app.turns[1].md
        drawing = peak = draws = 0
        append = md.append

        async def counted(text: str) -> None:
            nonlocal drawing, peak, draws
            draws += 1
            drawing += 1
            peak = max(peak, drawing)
            try:
                await append(text)
            finally:
                drawing -= 1

        md.append = counted  # type: ignore[assignment,method-assign]
        pieces = [reply[i : i + 5] for i in range(0, len(reply), 5)]
        for piece in pieces:
            app.tutor_delta(1, piece)
            await asyncio.sleep(0.004)
        app.tutor_end(1, "complete")
        for _ in range(40):
            if md.caught_up and 1 not in app._dirty:
                break
            await pilot.pause(0.05)
        assert md.caught_up and peak == 1  # one draw at a time, and all of it is on screen
        assert draws < len(pieces)  # text that arrives between draws goes out together
        full = Markdown(reply)
        await app._log_view().mount(full)
        await pilot.pause(0.2)
        shape = [(type(b).__name__, b.source_range) for b in md.children]
        assert shape == [(type(b).__name__, b.source_range) for b in full.children]  # same as drawing it whole


async def test_ui_says_thinking_then_writing_until_the_reply_is_complete() -> None:
    """Learners took a half-written reply for a finished one: nothing near the text said it was still coming."""
    from tests.unit.test_ui_text import info

    app = MentorApp(lambda ui: Idle())  # type: ignore[arg-type,return-value]
    async with app.run_test(size=(80, 30)) as pilot:
        live = app.query_one("#activity", Line)
        box = app.query_one("#input", PromptInput)
        assert not live.has_class("-on")
        app.tutor_begin(1, "learner_message", "openai · deepseek-flash")
        await pilot.pause(0.1)
        assert live.has_class("-on") and "Thinking…" in str(live.render())  # before the first word
        assert "still replying" in str(box.placeholder) and "thinking" in line(app, "#state")
        app.tutor_delta(1, "Half of the ans")
        await pilot.pause(0.2)
        assert "Writing…" in str(live.render()) and "Esc to stop" in str(live.render())
        assert list(app._log_view().children)[-1] is live  # right under the text so far
        app.tutor_end(1, "complete")
        await pilot.pause(0.1)
        assert not live.has_class("-on") and "still replying" not in str(box.placeholder)
        app.status(info(busy=True))  # preparing a step: the tutor works, but there is no reply to show yet
        await pilot.pause(0.1)
        assert live.has_class("-on") and "Thinking…" in str(live.render())
        question = asyncio.ensure_future(app.ask("Send context to the model provider?", "", [("n", "No")]))
        await pilot.pause(0.2)
        assert not live.has_class("-on")  # waiting for your answer is not “thinking”
        await pilot.press("n")
        assert await question == "n"
        await pilot.pause(0.1)
        assert live.has_class("-on") and "Thinking…  0s" in str(live.render())  # the count starts again
        app.status(info(busy=False))
        await pilot.pause(0.1)
        assert not live.has_class("-on")


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
