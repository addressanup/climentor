"""The tutor pane: a Textual app.

- Uses the terminal's own colours, so it matches Neovim and the shell beside it.
- A header shows the subject, the current step, and where you are in it
  (learn, try, check). The line under the header fills up as steps get done.
- Tutor replies stream in as Markdown (readable code blocks).
- Multiline input: Enter sends, Ctrl+J or Alt+Enter adds a new line. The grey
  text in the empty box says what you can type next.
- Esc cancels a reply. Ctrl+Q (or /quit) opens the quit menu.
- The status line uses words ("‖ paused"), so colour is never the only signal.
- Questions are keyboard dialogs: press the letter, or use the arrows and Enter.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import ClassVar, TypeVar

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Markdown, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from ..providers.mock import LABEL as MOCK_LABEL
from ..tutor import commands
from ..tutor.engine import Engine
from ..tutor.ui_protocol import StatusInfo
from .theme import mentor_theme, terminal_is_light
from .widgets import Line, ProgressRule, Segment

W = TypeVar("W", bound=Widget)

NOTE_GLYPHS = {"info": "·", "success": "✓", "warn": "!", "error": "✗"}
TRIGGER_WORDS = {
    "hint": "hint",
    "explain": "explanation",
    "lesson_intro": "lesson",
    "prediction": "your prediction",
    "verification": "check",
    "review": "review",
    "review_step": "review",
    "repeated_failure": "noticed a problem",
    "plan": "plan",
    "plan_revise": "plan",
    "tour": "welcome",
}
# status -> (glyph, words, style) shown under a reply that did not finish normally
STATUS_WORDS = {
    "cancelled": ("—", "cancelled", "dim"),
    "withdrawn": ("—", "withdrawn", "dim"),
    "superseded": ("—", "replaced", "dim"),
    "stale": ("!", "may be out of date", "$warning"),
    "error": ("✗", "error", "$error"),
}
STAGES = (("learn", "explain"), ("try", "attempt"), ("check", "verified"))
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


# ---------------------------------------------------------------- what the header and hints say
def stage_segments(stage: str | None) -> list[Segment]:
    """learn → try → check, marking what is done (✓), current (●) and to come (○)."""
    index = {"explain": 0, "attempt": 1, "verified": len(STAGES)}.get(stage or "", -1)
    out: list[Segment] = []
    for i, (word, _) in enumerate(STAGES):
        if out:
            out.append(("  ", ""))
        if i < index:
            out.append((f"✓ {word}", "dim"))
        elif i == index:
            out.append((f"● {word}", "bold $accent"))
        else:
            out.append((f"○ {word}", "dim"))
    return out


def header_segments(info: StatusInfo | None) -> tuple[list[Segment], list[Segment], list[Segment], list[Segment]]:
    """(subject, where in the plan, step, stage) for the two header lines."""
    if info is None:
        return [("CLI Mentor", "bold")], [], [("Getting ready…", "dim")], []
    if info.phase == "plan_review":
        return (
            [(info.subject or "Your plan", "bold")],
            [(f"proposed plan · {info.steps} steps", "dim")],
            [("Read it below. Reply ", "dim"), ("yes", "bold"), (" to start, or say what to change.", "dim")],
            [],
        )
    if info.subject is None:
        return [("New subject", "bold")], [], [("Tell me what you want to learn.", "dim")], []
    if info.step is None:
        return (
            [(info.subject, "bold")],
            [(f"{info.done} of {info.steps} steps done", "dim")],
            [("Every step is done.", "")],
            [("✓ complete", "$success")],
        )
    step: list[Segment] = [(info.step_title or "", "")]
    if info.step_stage in (None, "verified"):
        step.append(("  · not started", "dim"))
    return (
        [(info.subject, "bold")],
        [(f"step {info.step} of {info.steps}", "dim")],
        step,
        stage_segments(info.step_stage if info.step_stage != "verified" else None),
    )


def input_hints(info: StatusInfo | None) -> tuple[str, str]:
    """(grey text inside the empty input box, key hints under it)."""
    quit_ = "Ctrl+Q quit"
    if info is None:
        return "Type a message…", f"Enter send · Ctrl+J new line · {quit_}"
    if info.phase == "plan_review":
        return "Say yes to start, or say what to change", f"say “yes” to start · or say what to change · {quit_}"
    if info.subject is None:
        return (
            "What do you want to learn? For example: C++ from the very basics",
            f"Enter send · Ctrl+J new line · {quit_}",
        )
    if info.step is None:
        return "Say “new subject:” and a topic, or ask for a review", f"just type · {quit_}"
    if info.step_stage == "explain":
        return "Your prediction: what do you think will happen?", f"answer in your own words · “skip” · {quit_}"
    if info.step_stage == "attempt":
        return "Tell me when you are done, or ask a question", f"“done” to check · “hint” if stuck · {quit_}"
    return "Say “next” to start this step, or ask a question", f"“next” to start · “show the plan” · {quit_}"


def state_segments(info: StatusInfo | None, busy: bool, frame: int) -> list[Segment]:
    """Bottom-right status: writing, observation, sync problems, model. Words first, colour second."""
    out: list[Segment] = []
    if busy:
        out += [(f"{SPINNER[frame % len(SPINNER)]} writing", "$accent"), ("  ", "")]
    if info is None:
        return out
    style = {"on": "$success", "paused": "$warning"}.get(info.observation, "dim")
    out.append((info.observation_words, style))
    for problem in info.problems():
        out += [(" · ", "dim"), (problem, "$warning")]
    if info.mode != "guided":
        out += [(" · ", "dim"), (f"{info.mode} mode", "")]
    out += [(" · ", "dim"), (info.model_words, "dim")]
    return out


def tutor_label(trigger: str, label: str, status: str | None = None) -> Content:
    """“Mentor · hint · deepseek-chat”: who spoke, why, and which model wrote it."""
    parts: list[Segment] = [("Mentor", "bold $accent")]
    why = TRIGGER_WORDS.get(trigger, "")
    source = "offline mock, not an AI" if label == MOCK_LABEL else ("" if label == "CLI Mentor" else label)
    for word in (why, source, "writing…" if status == "streaming" else ""):
        if word:
            parts += [(" · ", "dim"), (word, "dim")]
    return Content.assemble(*parts)


# ---------------------------------------------------------------- widgets
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
    """A keyboard question: press a choice's letter, or use the arrow keys and Enter."""

    DEFAULT_CSS = """
    ChoiceScreen { align: center middle; }
    ChoiceScreen #dialog {
        width: 90%; max-width: 80; height: auto; max-height: 90%;
        border: round $accent; padding: 1 2; background: $background;
    }
    ChoiceScreen #title { text-style: bold; color: $accent; margin-bottom: 1; }
    ChoiceScreen #body { height: auto; max-height: 18; scrollbar-size-vertical: 1; }
    ChoiceScreen #choices { height: auto; margin-top: 1; border: none; padding: 0; background: $background; }
    ChoiceScreen #choices:focus { border: none; background-tint: transparent; }
    ChoiceScreen #how { text-style: dim; margin-top: 1; }
    """

    def __init__(self, title: str, body: str, choices: list[tuple[str, str]]) -> None:
        super().__init__()
        self.title_text = title
        self.body = body
        self.choices = choices

    @property
    def escape_key(self) -> str:
        keys = [k for k, _ in self.choices]
        return "n" if "n" in keys else keys[-1]

    def compose(self) -> ComposeResult:
        escape_label = dict(self.choices)[self.escape_key]
        with Vertical(id="dialog"):
            yield Static(self.title_text, id="title", markup=False)
            with VerticalScroll(id="body"):
                yield Static(self.body, markup=False)
            yield OptionList(
                *(
                    Option(Content.assemble((f" {key} ", "reverse bold"), "  ", label), id=key)
                    for key, label in self.choices
                ),
                id="choices",
            )
            yield Static(f"Press a letter, or ↑ ↓ and Enter · Esc: {escape_label}", id="how", markup=False)

    def on_mount(self) -> None:
        options = self.query_one("#choices", OptionList)
        options.highlighted = 0
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option.id or self.escape_key)

    def on_key(self, event: events.Key) -> None:
        if event.key in {k for k, _ in self.choices}:
            event.stop()
            self.dismiss(event.key)
        elif event.key == "escape":
            event.stop()
            self.dismiss(self.escape_key)


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
    """One item in the conversation: a tutor reply, your message, or a short note."""

    DEFAULT_CSS = """
    Entry { height: auto; margin: 0 0 1 0; }
    Entry > .label { height: 1; }
    Entry > .meta { height: auto; display: none; }
    Entry > .meta.-shown { display: block; }
    Entry > Horizontal { height: auto; }
    Entry .glyph { width: 2; text-style: bold; }
    Entry .body { width: 1fr; }
    Entry Markdown { width: 1fr; }
    Entry.learner .glyph { color: $accent; }
    Entry.info .glyph { text-style: dim; }
    Entry.success .glyph { color: $success; }
    Entry.warn .glyph { color: $warning; }
    Entry.error .glyph { color: $error; }
    """

    def __init__(self, kind: str, text: str, *, label: Content | None = None, note: str = "info") -> None:
        super().__init__(classes=f"{kind} {note}" if kind == "note" else kind)
        self.kind = kind
        self.text = text
        self.note_kind = note
        self.label = Static(label or "", classes="label")
        self.md = LiveMarkdown(text)
        self.meta = Static("", classes="meta")

    def compose(self) -> ComposeResult:
        if self.kind == "tutor":
            yield self.label
            yield self.md
            yield self.meta
            return
        with Horizontal():
            yield Static("›" if self.kind == "learner" else NOTE_GLYPHS.get(self.note_kind, "·"), classes="glyph")
            yield Static(self.text, classes="body", markup=False) if self.kind == "learner" else self.md

    def set_meta(self, content: Content) -> None:
        self.meta.update(content)
        self.meta.set_class(bool(content.plain), "-shown")

    def plain(self) -> str:
        """The visible words (label, text, status line), for tests."""
        body = self.text if self.kind == "learner" else self.md.latest
        return "\n".join(s for s in (str(self.label.render()), body, str(self.meta.render())) if s)


class MentorApp(App[str]):
    TITLE = "CLI Mentor"
    CSS = """
    Screen { layout: vertical; }
    #top { height: auto; padding: 0 1; }
    #top > Horizontal { height: 1; }
    #subject, #step { width: 1fr; text-wrap: nowrap; text-overflow: ellipsis; }
    #where, #stages { width: auto; padding-left: 2; }
    #log {
        height: 1fr; padding: 1 1 0 1;
        scrollbar-size-vertical: 1; scrollbar-background: $background;
        scrollbar-color: $mentor-rule; scrollbar-color-hover: $accent; scrollbar-color-active: $accent;
    }
    #log Markdown { padding: 0; margin: 0; }
    #log Markdown > MarkdownBlock:last-child { margin-bottom: 0; }
    #log MarkdownH1, #log MarkdownH2, #log MarkdownH3, #log MarkdownH4 { content-align: left top; margin: 0 0 1 0; }
    #log MarkdownFence { border-left: solid $mentor-rule; margin: 0 0 1 0; }
    #log MarkdownFence > Label { padding: 0 1; }
    #log MarkdownBlockQuote { border-left: outer $accent; background: transparent; }
    #log MarkdownBlock > .code_inline { color: $mentor-code; text-style: bold; background: transparent; }
    #input {
        height: auto; min-height: 3; max-height: 10; margin: 0 1;
        border: round $border-blurred; background: $background;
    }
    #input:focus { border: round $accent; }
    #input .text-area--cursor-line { background: transparent; }
    #input .text-area--placeholder { text-style: dim; }
    #bar { height: 1; padding: 0 2; }
    #keys { width: 1fr; text-style: dim; text-wrap: nowrap; text-overflow: ellipsis; }
    #state { width: auto; padding-left: 2; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel reply", show=False),
        Binding("ctrl+q", "quit_menu", "Quit", priority=True),
        Binding("ctrl+l", "focus_input", "Focus input", show=False),
        # priority: the text box would otherwise take these keys, so the conversation could not scroll
        Binding("pageup", "scroll_log(-1)", "Scroll up", show=False, priority=True),
        Binding("pagedown", "scroll_log(1)", "Scroll down", show=False, priority=True),
    ]

    def __init__(
        self,
        engine_factory: Callable[[MentorApp], Engine],
        *,
        on_detach: Callable[[], Awaitable[None]] | None = None,
        startup_notes: list[tuple[str, str]] | None = None,
        light: bool | None = None,
    ) -> None:
        super().__init__()
        self.engine_factory = engine_factory
        self.on_detach = on_detach
        self.startup_notes = startup_notes or []
        self.engine: Engine | None = None
        self.turns: dict[int, tuple[Entry, list[str], str, str]] = {}  # entry, parts, trigger, label
        self._dirty: set[int] = set()
        self._streaming: set[int] = set()
        self._frame = 0
        self._keys = ""
        self._last_status: StatusInfo | None = None
        self.register_theme(mentor_theme(terminal_is_light() if light is None else light))
        self.theme = "cli-mentor"

    # ------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        with Vertical(id="top"):
            with Horizontal():
                yield Line(id="subject")
                yield Line(id="where")
            with Horizontal():
                yield Line(id="step")
                yield Line(id="stages")
        yield ProgressRule(id="rule")
        yield VerticalScroll(id="log")
        yield PromptInput(id="input", soft_wrap=True, show_line_numbers=False)
        with Horizontal(id="bar"):
            yield Line(id="keys")
            yield Line(id="state")

    async def on_mount(self) -> None:
        self._show_status(None)
        # Stay at the newest message as text arrives; scrolling up lets go, scrolling to the end holds on again.
        self.query_one("#log", VerticalScroll).anchor()
        self.query_one("#input", PromptInput).focus()
        self.set_interval(0.08, self._tick)
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
    def _main(self, selector: str, kind: type[W]) -> W:
        """A widget on the main screen, even while a question dialog is on top."""
        return self.screen_stack[0].query_one(selector, kind)

    def _log_view(self) -> VerticalScroll:
        return self._main("#log", VerticalScroll)

    def _append(self, entry: Entry) -> None:
        self._log_view().mount(entry)  # the anchored log follows new text unless you scrolled up

    def tutor_begin(self, turn_id: int, trigger: str, label: str) -> None:
        entry = Entry("tutor", "", label=tutor_label(trigger, label, "streaming"))
        self.turns[turn_id] = (entry, [], trigger, label)
        self._streaming.add(turn_id)
        self._append(entry)

    def tutor_delta(self, turn_id: int, text: str) -> None:
        if turn_id in self.turns:
            self.turns[turn_id][1].append(text)
            self._dirty.add(turn_id)

    def tutor_end(self, turn_id: int, status: str, note: str = "") -> None:
        self._streaming.discard(turn_id)
        if turn_id not in self.turns:
            return
        entry, parts, trigger, label = self.turns[turn_id]
        if status == "complete" and not "".join(parts).strip() and not note:
            entry.remove()  # the tutor only took an action (such as running the check); nothing to read
            del self.turns[turn_id]
            self._dirty.discard(turn_id)
            return
        entry.label.update(tutor_label(trigger, label))
        glyph, words, style = STATUS_WORDS.get(status, ("", "", ""))
        if words:
            entry.set_meta(Content.assemble((f"{glyph} {words}", style), (f" — {note}" if note else "", style)))
        elif note:
            entry.set_meta(Content.assemble((note, "dim")))
        text = "".join(parts)
        entry.md.set_text(text or ("_(stopped before any text)_" if words else "_(no text)_"))
        self._dirty.discard(turn_id)

    def _tick(self) -> None:
        for turn_id in list(self._dirty):
            entry, parts, _trigger, _label = self.turns[turn_id]
            entry.md.set_text("".join(parts))
        self._dirty.clear()
        if self._busy:
            self._frame += 1
            with contextlib.suppress(NoMatches):  # the screen is gone while the app closes
                self._show_state()

    @property
    def _busy(self) -> bool:
        return bool(self._streaming) or bool(self._last_status and self._last_status.busy)

    def note(self, text: str, kind: str = "info") -> None:
        self._append(Entry("note", text, note=kind))

    def status(self, info: StatusInfo) -> None:
        with contextlib.suppress(NoMatches):  # the screen is gone while the app closes
            self._show_status(info)

    def _show_status(self, info: StatusInfo | None) -> None:
        self._last_status = info
        subject, where, step, stages = header_segments(info)
        for wid, segments in (("#subject", subject), ("#where", where), ("#step", step), ("#stages", stages)):
            self._main(wid, Line).show(segments)
        rule = self._main("#rule", ProgressRule)
        learning = info is not None and info.phase != "plan_review"
        rule.total = info.steps if info is not None and learning else 0
        rule.done = info.done if info is not None else 0
        rule.current = (info.step or 0) if info is not None else 0
        placeholder, self._keys = input_hints(info)
        self._main("#input", PromptInput).placeholder = placeholder
        self._show_state()

    def _show_state(self) -> None:
        keys = "Esc stop the reply · " + self._keys if self._busy else self._keys
        self._main("#keys", Line).show([(keys, "")])
        self._main("#state", Line).show(state_segments(self._last_status, self._busy, self._frame))

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
        self._append(Entry("learner", text))
        self._log_view().anchor()  # you just spoke: follow the conversation again, even if you had scrolled up
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
            self.action_focus_input()

    def action_focus_input(self) -> None:
        if len(self.screen_stack) == 1:  # leave a question dialog in charge of the keyboard
            self._main("#input", PromptInput).focus()

    def action_scroll_log(self, direction: int) -> None:
        log = self._log_view()
        step = direction * max(1, log.size.height - 2)
        if direction > 0 and log.scroll_y + step >= log.max_scroll_y:
            log.anchor()  # reached the end: follow new text again
        else:
            log.scroll_relative(y=step, animate=False)

    async def action_quit_menu(self) -> None:
        self.run_worker(self._quit_flow(), exclusive=True, group="quit")

    async def _quit_flow(self) -> None:
        engine = self.engine
        choice = await self.ask(
            "Leave CLI Mentor?",
            "Detach: everything keeps running. Come back later with `mentor resume`.\n"
            "End session: stop the tutor and close this session's panes. Your progress is saved, "
            "and I ask first if a file is unsaved.",
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
