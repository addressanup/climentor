"""The tutor pane: a Textual app.

- Streaming tutor replies rendered as Markdown (readable code blocks).
- Multiline input: Enter sends, Ctrl+J or Alt+Enter adds a new line.
- Esc cancels a reply. Ctrl+Q (or /quit) opens the quit menu.
- Status line uses words ("OBS: PAUSED"), so colour is never the only signal.
- Approval prompts are keyboard dialogs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import ClassVar

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Markdown, Static, TextArea

from ..tutor import commands
from ..tutor.engine import Engine
from ..tutor.ui_protocol import StatusInfo

NOTE_LABELS = {"info": "note", "success": "ok", "warn": "warning", "error": "error"}
STATUS_LABELS = {
    "complete": "",
    "streaming": "writing…",
    "cancelled": "[cancelled]",
    "withdrawn": "[withdrawn]",
    "superseded": "[replaced]",
    "stale": "[may be out of date]",
    "error": "[error]",
}


class PromptInput(TextArea):
    """TextArea where Enter submits and Ctrl+J / Alt+Enter insert a newline."""

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            text = self.text.strip()
            if text:
                self.post_message(self.Submitted(text))
                self.text = ""
            return
        if event.key in ("ctrl+j", "alt+enter", "shift+enter"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        await super()._on_key(event)


class ChoiceScreen(ModalScreen[str]):
    """A keyboard-only question: press the letter shown next to a choice."""

    DEFAULT_CSS = """
    ChoiceScreen { align: center middle; }
    #dialog { width: 90%; max-width: 90; height: auto; border: heavy $warning; padding: 1 2; background: $surface; }
    #title { text-style: bold; margin-bottom: 1; }
    #choices { margin-top: 1; text-style: bold; }
    """

    def __init__(self, title: str, body: str, choices: list[tuple[str, str]]) -> None:
        super().__init__()
        self.title_text = title
        self.body = body
        self.choices = choices

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(f"QUESTION: {self.title_text}", id="title")
            yield Static(self.body, markup=False)
            yield Static("   ".join(f"[{k}] {label}" for k, label in self.choices), id="choices", markup=False)

    def on_key(self, event: events.Key) -> None:
        keys = {k for k, _ in self.choices}
        if event.key in keys:
            self.dismiss(event.key)
        elif event.key == "escape":
            self.dismiss("n" if "n" in keys else self.choices[-1][0])
        event.stop()


class LiveMarkdown(Markdown):
    """Markdown that keeps the newest text even if it changes before mounting.

    (Markdown re-applies its constructor text on mount, which would otherwise
    wipe a fast reply that finished before the widget was mounted.)
    """

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.latest = text

    def set_text(self, text: str) -> None:
        self.latest = text
        # Markdown applies ``_initial_markdown`` when it mounts (Textual 8.x).
        self._initial_markdown = text
        if self.is_mounted:
            self.update(text)
        else:
            self.call_after_refresh(self._sync)

    def _sync(self) -> None:
        if self.source != self.latest:
            self.update(self.latest)


class Entry(Vertical):
    DEFAULT_CSS = """
    Entry { height: auto; margin: 0 0 1 0; }
    Entry .label { text-style: bold; }
    Entry.learner .label { color: $accent; }
    Entry.tutor .label { color: $success; }
    Entry.note .label { color: $secondary; }
    Entry.warn .label, Entry.error .label { color: $error; }
    Entry Markdown { margin: 0; padding: 0 1; }
    """

    def __init__(self, label: str, text: str, classes: str) -> None:
        super().__init__(classes=classes)
        self.label = Static(label, classes="label", markup=False)
        self.md = LiveMarkdown(text)

    def compose(self) -> ComposeResult:
        yield self.label
        yield self.md


class MentorApp(App[str]):
    TITLE = "CLI Mentor"
    CSS = """
    Screen { layout: vertical; }
    #status { height: auto; background: $panel; color: $text; padding: 0 1; }
    #log { height: 1fr; padding: 0 1; }
    #input { height: 5; border: round $accent; }
    #keys { height: 1; color: $text-muted; padding: 0 1; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel reply", show=False),
        Binding("ctrl+q", "quit_menu", "Quit", priority=True),
        Binding("ctrl+l", "focus_input", "Focus input", show=False),
        Binding("pageup", "scroll_log(-1)", "Scroll up", show=False),
        Binding("pagedown", "scroll_log(1)", "Scroll down", show=False),
    ]

    def __init__(
        self,
        engine_factory: Callable[[MentorApp], Engine],
        *,
        on_detach: Callable[[], Awaitable[None]] | None = None,
        startup_notes: list[tuple[str, str]] | None = None,
    ) -> None:
        super().__init__()
        self.engine_factory = engine_factory
        self.on_detach = on_detach
        self.startup_notes = startup_notes or []
        self.engine: Engine | None = None
        self.turns: dict[int, tuple[Entry, list[str]]] = {}
        self._dirty: set[int] = set()
        self._last_status: StatusInfo | None = None

    # ------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Static("Starting…", id="status", markup=False)
        yield VerticalScroll(id="log")
        yield PromptInput(id="input", soft_wrap=True, show_line_numbers=False)
        yield Static(
            "Enter send · Ctrl+J new line · Esc cancel · /help commands · Ctrl+Q quit", id="keys", markup=False
        )

    async def on_mount(self) -> None:
        self.query_one("#input", PromptInput).focus()
        self.set_interval(0.08, self._flush_streams)
        for kind, text in self.startup_notes:
            self.note(text, kind)
        self.engine = self.engine_factory(self)
        self.run_worker(self._start_engine(), exclusive=False, group="engine")

    async def _start_engine(self) -> None:
        assert self.engine is not None
        try:
            await self.engine.start()
        except Exception as exc:  # keep the pane alive and explain
            self.note(f"The tutor engine failed to start: {exc}", "error")

    # ------------------------------------------------------------ UiSink
    def _log_view(self) -> VerticalScroll:
        return self.query_one("#log", VerticalScroll)

    def _append(self, entry: Entry) -> None:
        log = self._log_view()
        at_bottom = log.scroll_offset.y >= log.max_scroll_y - 2
        log.mount(entry)
        if at_bottom:
            self.call_after_refresh(log.scroll_end, animate=False)

    def tutor_begin(self, turn_id: int, trigger: str, label: str) -> None:
        entry = Entry(f"Tutor — {label}  writing…", "", "tutor")
        self.turns[turn_id] = (entry, [])
        self._append(entry)

    def tutor_delta(self, turn_id: int, text: str) -> None:
        if turn_id in self.turns:
            self.turns[turn_id][1].append(text)
            self._dirty.add(turn_id)

    def tutor_end(self, turn_id: int, status: str, note: str = "") -> None:
        if turn_id not in self.turns:
            return
        entry, parts = self.turns[turn_id]
        label = str(entry.label.render()).replace("  writing…", "")
        suffix = STATUS_LABELS.get(status, f"[{status}]")
        entry.label.update(f"{label}  {suffix}".rstrip())
        text = "".join(parts)
        if note:
            text += f"\n\n> **{suffix.strip('[]') or 'note'}:** {note}"
        entry.md.set_text(text or "_(no text)_")
        self._dirty.discard(turn_id)
        self._log_view().scroll_end(animate=False)

    def _flush_streams(self) -> None:
        for turn_id in list(self._dirty):
            entry, parts = self.turns[turn_id]
            entry.md.set_text("".join(parts))
        if self._dirty:
            self._log_view().scroll_end(animate=False)
        self._dirty.clear()

    def note(self, text: str, kind: str = "info") -> None:
        label = NOTE_LABELS.get(kind, kind)
        self._append(Entry(f"[{label}]", text, f"note {kind}"))

    def status(self, info: StatusInfo) -> None:
        self._last_status = info
        line = info.line()
        perms = ",".join(p.replace("observe_", "obs-").replace("run_", "") for p in info.permissions) or "none"
        self.query_one("#status", Static).update(f"{line}\nPERMISSIONS: {perms}")

    async def ask(self, title: str, body: str, choices: list[tuple[str, str]]) -> str:
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        def done(result: str | None) -> None:
            if not future.done():
                future.set_result(result or "n")

        await self.push_screen(ChoiceScreen(title, body, choices), callback=done)
        return await future

    # ------------------------------------------------------------ input
    async def on_prompt_input_submitted(self, message: PromptInput.Submitted) -> None:
        text = message.text
        self._append(Entry("You", text if not text.startswith("/") else f"`{text}`", "learner"))
        self.run_worker(self._handle(text), exclusive=False, group="input")

    async def _handle(self, text: str) -> None:
        engine = self.engine
        if engine is None:
            return
        try:
            if text.startswith("/"):
                result = await commands.dispatch(engine, text)
                if result == commands.Result.QUIT:
                    await self.action_quit_menu()
            else:
                await engine.learner_message(text)
        except Exception as exc:
            self.note(f"Something went wrong: {type(exc).__name__}: {exc}", "error")

    # ------------------------------------------------------------ actions
    async def action_cancel(self) -> None:
        if self.engine is not None and not await self.engine.cancel_turn("cancelled", "stopped by you"):
            self.query_one("#input", PromptInput).focus()

    def action_focus_input(self) -> None:
        self.query_one("#input", PromptInput).focus()

    def action_scroll_log(self, direction: int) -> None:
        log = self._log_view()
        log.scroll_relative(y=direction * max(1, log.size.height - 2), animate=False)

    async def action_quit_menu(self) -> None:
        self.run_worker(self._quit_flow(), exclusive=True, group="quit")

    async def _quit_flow(self) -> None:
        engine = self.engine
        choice = await self.ask(
            "Leave CLI Mentor?",
            "Detach: leave everything running; come back later with `mentor resume`.\n"
            "End session: stop the tutor and close this session's panes (asks first if work is unsaved).",
            [("d", "Detach"), ("e", "End session"), ("c", "Cancel")],
        )
        if choice == "d":
            if self.on_detach is not None:
                await self.on_detach()
            else:
                self.exit("detach")
            return
        if choice != "e":
            return
        if engine is not None:
            risks = engine.unsaved_summary()
            if risks:
                confirm = await self.ask(
                    "Unsaved work!",
                    "Ending the session closes the editor and shell panes:\n- "
                    + "\n- ".join(risks)
                    + "\n\nSave in Neovim first (Esc, then :w). End anyway and LOSE these changes?",
                    [("n", "No, go back"), ("y", "Yes, end and lose them")],
                )
                if confirm != "y":
                    return
            await engine.stop()
            engine.sessions.end(engine.session_id)
        self.exit("end")
