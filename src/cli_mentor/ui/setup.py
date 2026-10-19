"""Full-screen first-time setup (`mentor init` at a terminal).

Five short steps: practice folder, about you, how the tutor helps, AI model,
and review. Nothing is saved until the review step, except an API key you
paste or a ChatGPT sign-in: those go straight into the macOS Keychain, never
into a file.

Keys: Enter continues, Space ticks a box, ↑ ↓ choose, Esc goes back.
"""

from __future__ import annotations

import subprocess
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Protocol, cast

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Input, LoadingIndicator, OptionList, SelectionList, Static
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

from ..apikeys import get_api_key, set_api_key
from ..chatgpt_auth import PLAN_OFF, USAGE_URL, Account, ChatGPTAuth, SignInError
from ..config import Config, ProviderName
from ..db.store import open_store
from ..memory.profile import Profile
from ..onboarding import SetupAnswers, apply_answers, fetch_models, proposed_facts, server_address
from ..paths import AppPaths
from ..providers.base import ProviderError
from .theme import mentor_theme, terminal_is_light
from .widgets import Line, ProgressRule, Segment

FetchModels = Callable[[Config, ProviderName], Awaitable[list[str]]]


class ChatGPTSignIn(Protocol):
    """What setup needs from ChatGPT sign-in (``ChatGPTAuth``, or a fake in tests)."""

    open_browser: Callable[[str], object]

    def account(self) -> Account | None: ...

    async def sign_in(self, on_url: Callable[[str], None] | None = None, *, timeout: float = 600.0) -> Account: ...


STEPS = ("Practice folder", "About you", "How I help", "AI model", "Review")
STEP_NOTES = (
    "where your work lives",
    "so I pitch things right",
    "how often I speak up",
    "the brain behind the tutor",
    "check, then save",
)
STYLES = (
    ("balanced", "Balanced", "I speak up when you seem stuck, for example after two failed runs."),
    ("quiet", "Quiet", "I only answer when you ask."),
    ("proactive", "Proactive", "I point things out sooner."),
)
PROVIDERS = (
    ("chatgpt", "ChatGPT plan: sign in with your ChatGPT account, no API key (the plan Codex uses)"),
    ("anthropic", "Anthropic (Claude), with an API key"),
    ("openai", "OpenAI, with an API key"),
    ("compatible", "Another service that works like OpenAI (DeepSeek and others), with an API key"),
    ("mock", "Skip for now: offline demo only, it cannot make plans"),
)
PROVIDER_NAMES = {
    "chatgpt": "ChatGPT plan",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "compatible": "OpenAI-compatible service",
}


def ready_account(auth: ChatGPTSignIn) -> Account | None:
    """The ChatGPT sign-in, if it can already pay for tutor replies."""
    account = auth.account()
    return account if account is not None and account.signed_in and account.plan_enabled else None


@dataclass
class SetupResult:
    saved: bool
    start: bool = False


def describe_folder(text: str) -> tuple[bool, list[Segment]]:
    """Is this a usable practice folder? Returns (ok, message)."""
    if not text.strip():
        return False, [("✗ Type a folder path.", "$error")]
    path = Path(text.strip()).expanduser()
    if path.is_dir():
        return True, [("✓ ", "$success"), ("This folder exists. I will use it as it is.", "dim")]
    if path.exists():
        return False, [("✗ This is a file, not a folder.", "$error")]
    return True, [("This folder will be created when you save. Only you can open it.", "dim")]


def short_path(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home) :] if path == home or path.startswith(home + "/") else path


# ---------------------------------------------------------------- widgets with “Enter means continue”
class ToggleList(SelectionList[str], inherit_bindings=False):
    """Space ticks or unticks a line. Enter is left free to mean “continue”."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("space", "select", "Toggle", show=False),
        Binding("down", "cursor_down", show=False),
        Binding("up", "cursor_up", show=False),
        Binding("home", "first", show=False),
        Binding("end", "last", show=False),
        Binding("pageup", "page_up", show=False),
        Binding("pagedown", "page_down", show=False),
    ]


class FilterInput(Input):
    """A text box where ↓ moves into the list below it."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("down", "app.focus_next", show=False)]


class Tick(Checkbox, inherit_bindings=False):
    """A checkbox that Space ticks. Enter is left free to mean “continue”."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("space", "toggle_button", "Toggle", show=False)]


class EditScreen(ModalScreen[str | None]):
    """Change the words of one profile line. Enter saves, Esc cancels."""

    DEFAULT_CSS = """
    EditScreen { align: center middle; }
    EditScreen #dialog { width: 90%; max-width: 80; height: auto; border: round $accent; padding: 1 2; background: $background; }
    EditScreen #title { text-style: bold; color: $accent; margin-bottom: 1; }
    EditScreen #how { text-style: dim; margin-top: 1; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", show=False)]

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("Say it your way", id="title")
            yield Input(self.text, id="edit")
            yield Static("Enter saves · Esc cancels", id="how")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------- one page of setup
class Step(Screen[None]):
    """A setup page: header with progress, the page body, Back / Continue buttons, key hints."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "next_field", show=False),
        Binding("escape", "back", show=False),
    ]
    AUTO_FOCUS = None  # on_mount picks the first field itself
    number = 0  # 1-5 for the counted steps, 0 for welcome and done
    heading = ""
    next_label = "Continue"
    back_label: str | None = "Back"
    keys = "Enter continue · Esc back · Ctrl+C quit"

    @property
    def setup(self) -> SetupApp:
        return cast(SetupApp, self.app)

    def compose(self) -> ComposeResult:
        with Vertical(id="page"):
            with Horizontal(id="top"):
                yield Line(id="brand")
                yield Line(id="count")
            yield ProgressRule(id="rule")
            with VerticalScroll(id="body", can_focus=False):
                yield Static(self.heading, id="heading", markup=False)
                yield from self.body()
                next_label, back_label = self.button_labels()
                with Horizontal(id="buttons"):
                    yield Button(next_label, id="next")
                    if back_label:
                        yield Button(back_label, id="back")
            yield Line(id="keys")

    def body(self) -> Iterator[Widget]:
        yield from ()

    def button_labels(self) -> tuple[str, str | None]:
        return self.next_label, self.back_label

    def key_hints(self) -> str:
        return self.keys

    def on_mount(self) -> None:
        self.query_one("#brand", Line).show([("CLI Mentor", "bold $accent"), ("  setup", "dim")])
        if self.number:
            self.query_one("#count", Line).show([(f"step {self.number} of {len(STEPS)}", "dim")])
        rule = self.query_one("#rule", ProgressRule)
        rule.total = len(STEPS)
        rule.done = len(STEPS) if isinstance(self, DoneStep) else max(0, self.number - 1)
        rule.current = self.number
        self.query_one("#keys", Line).show([(self.key_hints(), "dim")])
        self.call_after_refresh(self._focus_first)

    def _focus_first(self) -> None:
        fields = self.fields()
        (fields[0] if fields else self.query_one("#next", Button)).focus()

    def fields(self) -> list[Widget]:
        return [w for w in self.focus_chain if isinstance(w, (Input, OptionList, Checkbox))]

    def text(self, value: str | Content, classes: str = "text") -> Static:
        return Static(value, classes=classes, markup=False)

    def error(self, segments: list[Segment]) -> None:
        self.query_one("#error", Line).show(segments)

    # navigation -----------------------------------------------------
    def action_next_field(self) -> None:
        fields = self.fields()
        if not fields or self.focused not in fields or self.focused is fields[-1]:
            self.submit()
        else:
            self.focus_next()

    def action_back(self) -> None:
        self.app.pop_screen()

    @on(Input.Submitted)
    def _input_done(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_next_field()

    @on(OptionList.OptionSelected)
    def _choice_done(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.action_next_field()

    @on(Button.Pressed, "#next")
    def _next_pressed(self) -> None:
        self.submit()

    @on(Button.Pressed, "#back")
    def _back_pressed(self) -> None:
        self.action_back()

    def submit(self) -> None:
        raise NotImplementedError


class WelcomeStep(Step):
    heading = "Welcome to CLI Mentor"
    next_label = "Begin"
    back_label = None
    keys = "Enter begin · Ctrl+C quit"

    def body(self) -> Iterator[Widget]:
        yield self.text(
            "A tutor that sits beside your real terminal. You write the code in Neovim and the shell. "
            "I explain new ideas, give small hints, and check your work."
        )
        yield self.text("Setup has five short steps:", "text tight")
        rows: list[Segment] = []
        for i, (name, note) in enumerate(zip(STEPS, STEP_NOTES, strict=True), start=1):
            rows += [(f"  {i}  ", "bold $accent"), (f"{name:<17}", "bold"), (note, "dim"), ("\n", "")]
        yield self.text(Content.assemble(*rows[:-1]))
        yield self.text(
            "Nothing is installed. Files are only created in the folder you choose. "
            "Run mentor init again any time to change your answers."
        )

    def action_back(self) -> None:
        self.setup.finish(start=False)

    def submit(self) -> None:
        self.app.push_screen(FolderStep())


class FolderStep(Step):
    number = 1
    heading = "Where should your work live?"

    def body(self) -> Iterator[Widget]:
        yield self.text(
            "This is your practice folder. Each subject you learn gets its own folder inside it. "
            "It is the only folder I read."
        )
        yield Input(short_path(self.setup.answers.workspace), id="folder", placeholder="~/cli-mentor-workspace")
        yield Line(id="error")

    def on_mount(self) -> None:
        super().on_mount()
        self.error(describe_folder(self.query_one("#folder", Input).value)[1])

    @on(Input.Changed, "#folder")
    def _changed(self, event: Input.Changed) -> None:
        self.error(describe_folder(event.value)[1])

    def submit(self) -> None:
        value = self.query_one("#folder", Input).value
        ok, message = describe_folder(value)
        self.error(message)
        if ok:
            self.setup.answers.workspace = str(Path(value.strip()).expanduser())
            self.app.push_screen(AboutStep())


class AboutStep(Step):
    number = 2
    heading = "About you"
    BINDINGS: ClassVar[list[BindingType]] = [Binding("e", "edit", show=False)]

    def key_hints(self) -> str:
        if not self.setup.facts:
            return self.keys
        return "Space keep or remove · e change the words · Enter continue · Esc back"

    def body(self) -> Iterator[Widget]:
        app = self.setup
        if not app.facts:
            yield self.text("Your profile is already set. See or change it any time with /profile in the tutor.")
            return
        yield self.text(
            "These are my starting guesses about you. Untick any that are wrong, "
            "or press e to change the words. I only use the ones you keep."
        )
        yield ToggleList(
            *(
                Selection(app.edits.get(key, value), key, app.answers.facts.get(key, value) is not None, id=key)
                for key, value in app.facts
            ),
            id="facts",
        )

    def action_edit(self) -> None:
        facts = self.query("#facts")
        if not facts:
            return
        listing = cast(ToggleList, facts.first())
        index = listing.highlighted
        if index is None:
            return
        key = str(listing.get_option_at_index(index).value)
        current = self.setup.edits.get(key, dict(self.setup.facts)[key])

        def done(text: str | None) -> None:
            if text:
                self.setup.edits[key] = text
                listing.replace_option_prompt(key, text)

        self.app.push_screen(EditScreen(current), done)

    def submit(self) -> None:
        app = self.setup
        facts = self.query("#facts")
        if facts:
            kept = set(cast(ToggleList, facts.first()).selected)
            app.answers.facts = {k: (app.edits.get(k, v) if k in kept else None) for k, v in app.facts}
        self.app.push_screen(StyleStep())


class StyleStep(Step):
    number = 3
    heading = "How should I help?"
    keys = "↑ ↓ choose · Space tick · Enter next · Esc back"

    def body(self) -> Iterator[Widget]:
        answers = self.setup.answers
        yield self.text("How often may I speak up without being asked?", "label")
        styles = OptionList(
            *(Option(Content.assemble((name, "bold"), ("\n" + note, "dim")), id=key) for key, name, note in STYLES),
            id="style",
        )
        styles.highlighted = next((i for i, s in enumerate(STYLES) if s[0] == answers.intervention), 0)
        yield styles
        yield Tick(
            "Let me watch the shell and Neovim panes without asking at each start",
            answers.remember_observation,
            id="remember",
        )
        yield self.text("Anything else I should know about you? (optional)", "label")
        yield Input(answers.own_words, id="own", placeholder="For example: I already know some Python")

    def submit(self) -> None:
        answers = self.setup.answers
        index = self.query_one("#style", OptionList).highlighted or 0
        answers.intervention = STYLES[index][0]
        answers.remember_observation = self.query_one("#remember", Tick).value
        answers.own_words = self.query_one("#own", Input).value.strip()
        self.app.push_screen(ModelStep())


class ModelStep(Step):
    number = 4
    heading = "Which AI model should teach you?"
    keys = "↑ ↓ choose · Space tick · Enter next · Esc back"

    def body(self) -> Iterator[Widget]:
        app = self.setup
        yield self.text(
            "I need an AI model to plan lessons and answer you. Sign in with your ChatGPT plan, "
            "or use your own API key. I keep keys and sign-ins in the macOS Keychain, never in a file."
        )
        choices = OptionList(*(Option(label, id=key) for key, label in PROVIDERS), id="provider")
        choices.highlighted = next(i for i, p in enumerate(PROVIDERS) if p[0] == app.choice)
        yield choices
        yield Line(id="account", classes="label")
        with Vertical(id="address-box"):
            yield self.text("Service address", "label")
            yield Input(app.address, id="address", placeholder="https://api.deepseek.com")
        with Vertical(id="key-box"):
            yield Line(id="key-label", classes="label")
            yield Input(id="key", password=True)
        yield Tick(
            "Send my messages and what the tutor sees to this service without asking first",
            app.answers.allow_network,
            id="network",
        )
        yield Line(id="error")

    def on_mount(self) -> None:
        super().on_mount()
        self._show_fields()

    @property
    def choice(self) -> str:
        return PROVIDERS[self.query_one("#provider", OptionList).highlighted or 0][0]

    @on(OptionList.OptionHighlighted, "#provider")
    def _highlighted(self) -> None:
        self._show_fields()

    def _show_fields(self) -> None:
        choice = self.choice
        slot: ProviderName = "anthropic" if choice == "anthropic" else "openai"
        self.query_one("#address-box").display = choice == "compatible"
        self.query_one("#key-box").display = choice not in ("mock", "chatgpt")
        self.query_one("#account").display = choice == "chatgpt"
        self.query_one("#network").display = choice != "mock"
        self.error([])
        account = ready_account(self.setup.auth) if choice == "chatgpt" else None
        # OpenAI's wording for the action that starts ChatGPT sign-in.
        self.query_one("#next", Button).label = (
            "Continue with ChatGPT" if choice == "chatgpt" and account is None else "Continue"
        )
        if choice == "chatgpt":
            self.query_one("#account", Line).show(
                [
                    ("✓ ", "$success"),
                    (f"Signed in as {account.label}. ", "bold"),
                    ("Your plan pays for replies.", "dim"),
                ]
                if account
                else [("Your browser opens so you can sign in and allow CLI Mentor to use your plan.", "dim")]
            )
            return
        found = get_api_key(slot)
        key = self.query_one("#key", Input)
        if found.key:
            where = "the Keychain" if found.source == "keychain" else f"the {found.source}"
            shared = " (saved under OpenAI, shared with OpenAI-style services)" if choice == "compatible" else ""
            self.query_one("#key-label", Line).show(
                [("API key", "bold"), (f"  found in {where}{shared}. Leave empty to keep it.", "dim")]
            )
            key.placeholder = "Paste a new key only to replace it"
        else:
            self.query_one("#key-label", Line).show([("API key", "bold"), ("  hidden as you type", "dim")])
            key.placeholder = "Paste your API key"

    def fields(self) -> list[Widget]:
        return [w for w in super().fields() if w.display and (w.parent is None or w.parent.display)]

    def submit(self) -> None:
        app, choice = self.setup, self.choice
        changed, app.choice = choice != app.choice, choice
        if choice == "mock":
            app.answers.provider, app.answers.model, app.answers.allow_network = "mock", "", False
            self.app.push_screen(ReviewStep())
            return
        if choice == "chatgpt":
            app.answers.provider, app.answers.base_url = "chatgpt", None
            app.answers.allow_network = self.query_one("#network", Tick).value
            if changed:
                app.answers.model = app.config.provider_settings("chatgpt").model
            self.app.push_screen(ModelPickStep() if ready_account(app.auth) else SignInStep())
            return
        slot: ProviderName = "anthropic" if choice == "anthropic" else "openai"
        address = self.query_one("#address", Input).value.strip().rstrip("/")
        if choice == "compatible" and not address.startswith(("https://", "http://")):
            self.error([("✗ Type the service's address, starting with https://", "$error")])
            self.query_one("#address", Input).focus()
            return
        key_input = self.query_one("#key", Input)
        new_key = key_input.value.strip()
        if not new_key and get_api_key(slot).key is None:
            self.error([("✗ Paste your API key, or choose “Skip for now”.", "$error")])
            key_input.focus()
            return
        if new_key:
            set_api_key(slot, new_key)
            key_input.value = ""
        app.address = address
        app.answers.provider = slot
        app.answers.base_url = address if choice == "compatible" else ""
        app.config.provider_settings(slot).base_url = app.answers.base_url
        app.answers.allow_network = self.query_one("#network", Tick).value
        if changed:  # a model name from another provider would not make sense here
            app.answers.model = app.config.provider_settings(slot).model if choice != "compatible" else ""
        self.app.push_screen(ModelPickStep())


class SignInStep(Step):
    """Continue with ChatGPT: the browser signs in, this page waits and confirms."""

    number = 4
    heading = "Continue with ChatGPT"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("c", "copy_link", show=False),
        Binding("o", "open_link", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.url = ""
        self.account: Account | None = None
        self.waiting = False

    def button_labels(self) -> tuple[str, str | None]:
        return "Waiting…", "Back"

    def key_hints(self) -> str:
        if self.account:
            return "Enter continue · Esc back"
        if self.waiting:
            return "c copy the link · o open the browser again · Esc back"
        return "Enter try again · Esc back"

    def body(self) -> Iterator[Widget]:
        yield self.text(
            "Your browser is opening. Sign in to ChatGPT, then allow CLI Mentor to use your plan. "
            "Come back here when the browser says you are signed in.",
            "text intro",
        )
        yield LoadingIndicator(id="loading")
        yield Line(id="link", classes="text")
        yield Line(id="error")

    def on_mount(self) -> None:
        super().on_mount()
        self.start()

    def start(self) -> None:
        self.waiting = True
        self.error([])
        self.query_one("#loading").display = True
        self.query_one("#next", Button).label = "Waiting…"
        self.query_one("#keys", Line).show([(self.key_hints(), "dim")])
        self.run_worker(self._sign_in(), exclusive=True)

    def _got_url(self, url: str) -> None:
        self.url = url
        self.query_one("#link", Line).show(
            [("If the browser did not open, press c to copy the link, or o to try again.", "dim")]
        )

    async def _sign_in(self) -> None:
        try:
            account = await self.setup.auth.sign_in(self._got_url)
        except SignInError as exc:
            self.error([("✗ ", "$error"), (exc.message, "")])
        except Exception as exc:  # e.g. no local port for the browser to come back to
            self.error([("✗ ", "$error"), (f"Could not finish the sign-in ({type(exc).__name__}).", "")])
        else:
            if account.plan_enabled:
                self.account = account
                self.query_one("#heading", Static).update("You're using your ChatGPT plan")
                self.query_one(".intro", Static).update(
                    f"Signed in as {account.label}. Tutor replies will use your ChatGPT plan. "
                    f"Manage usage any time at {USAGE_URL}"
                )
                self.query_one("#link", Line).show([])
            else:
                self.error([("✗ ", "$error"), (PLAN_OFF, "")])
        # Not reached when Esc leaves this page: the sign-in is cancelled and the page is gone.
        self.waiting = False
        self.query_one("#loading").display = False
        self.query_one("#next", Button).label = "Got it" if self.account else "Try again"
        self.query_one("#keys", Line).show([(self.key_hints(), "dim")])
        self.query_one("#next", Button).focus()

    def action_copy_link(self) -> None:
        if not self.url:
            return
        try:
            subprocess.run(["pbcopy"], input=self.url.encode(), check=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            self.app.copy_to_clipboard(self.url)
        self.query_one("#link", Line).show([("✓ ", "$success"), ("Link copied. Paste it into your browser.", "dim")])

    def action_open_link(self) -> None:
        if self.url:
            self.setup.auth.open_browser(self.url)

    def submit(self) -> None:
        if self.account:
            self.app.push_screen(ModelPickStep())
        elif not self.waiting:
            self.start()


class ModelPickStep(Step):
    number = 4
    heading = "Choose a model"
    keys = "Type to filter · ↓ the list · Enter choose · Esc back"

    def __init__(self) -> None:
        super().__init__()
        self.models: list[str] = []

    def body(self) -> Iterator[Widget]:
        yield Line(id="where", classes="text")
        yield LoadingIndicator(id="loading")
        yield FilterInput(self.setup.answers.model, id="model", placeholder="Type to filter, or type a model id")
        yield OptionList(id="models")
        yield Line(id="error")

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#models").display = False
        chatgpt = self.setup.answers.provider == "chatgpt"
        where = "ChatGPT" if chatgpt else server_address(self.setup.config, self.setup.answers.provider)
        how = (
            " which models your plan includes." if chatgpt else " which models it offers. This signs in with your key."
        )
        self.query_one("#where", Line).show([("Asking ", "dim"), (where, "bold"), (how, "dim")])
        self.run_worker(self._load(where), exclusive=True)

    async def _load(self, where: str) -> None:
        app = self.setup
        try:
            self.models = await app.fetch(app.config, app.answers.provider)
            self.query_one("#where", Line).show(
                [(where, "bold"), (f" offers {len(self.models)} models. Pick one, or type to filter.", "dim")]
            )
        except ProviderError as exc:
            if app.answers.provider == "chatgpt":
                text = exc.user_message()
            elif exc.kind == "auth":
                text = (
                    exc.message
                    + " Check that the key belongs to this service and the address is right (Esc goes back)."
                )
            else:
                text = exc.message
            self.error([("✗ ", "$error"), (text, ""), ("\nYou can still type a model id above.", "dim")])
        except Exception as exc:  # network trouble of any kind: explain, let them type an id
            self.error([("✗ ", "$error"), (f"Could not reach {where} ({type(exc).__name__}).", "")])
        finally:
            self.query_one("#loading").display = False
        self._filter(self.query_one("#model", Input).value)

    @on(Input.Changed, "#model")
    def _typed(self, event: Input.Changed) -> None:
        self._filter(event.value)

    def _filter(self, text: str) -> None:
        listing = self.query_one("#models", OptionList)
        shown = [m for m in self.models if text.strip().lower() in m.lower()]  # none match: use what they typed
        listing.clear_options()
        listing.add_options([Option(m, id=m) for m in shown[:200]])
        listing.display = bool(shown)
        if shown:
            listing.highlighted = shown.index(text.strip()) if text.strip() in shown else 0

    def _chosen(self) -> str:
        typed = self.query_one("#model", Input).value.strip()
        listing = self.query_one("#models", OptionList)
        if typed in self.models or (typed and not listing.display):
            return typed
        if listing.display and listing.highlighted is not None:
            return str(listing.get_option_at_index(listing.highlighted).id)
        return typed

    def action_next_field(self) -> None:
        self.submit()

    def submit(self) -> None:
        model = self._chosen()
        if not model:
            self.error([("✗ Pick a model, or type its id.", "$error")])
            return
        self.setup.answers.model = model
        self.app.push_screen(ReviewStep())


class ReviewStep(Step):
    number = 5
    heading = "Check, then save"
    next_label = "Save"
    keys = "Enter save · Esc back · Ctrl+C quit"

    def body(self) -> Iterator[Widget]:
        app, a = self.setup, self.setup.answers
        folder = short_path(a.workspace) + ("" if Path(a.workspace).is_dir() else "  (will be created)")
        if a.facts:
            values = list(a.facts.values())
            kept = sum(v is not None for v in values)
            changed = sum(1 for k, v in a.facts.items() if v is not None and v != dict(app.facts).get(k))
            profile = f"{kept} kept ({changed} in your words), {len(values) - kept} removed"
        else:
            profile = "already set"
        account = ready_account(app.auth) if a.provider == "chatgpt" else None
        if a.provider == "mock":
            model = "none yet: offline demo only"
        else:
            where = f" at {a.base_url}" if a.base_url else f" ({account.label})" if account else ""
            model = f"{PROVIDER_NAMES[app.choice]} · {a.model}{where}"
        rows = [
            ("Practice folder", folder),
            ("About you", profile),
            ("Speaking up", dict((k, n) for k, n, _ in STYLES).get(a.intervention or "balanced", "Balanced")),
            ("Watching panes", "remembered" if a.remember_observation else "I ask at each start"),
            ("Your note", a.own_words or "—"),
            ("AI model", model),
        ]
        if a.provider != "mock":
            rows.append(("Sending to it", "allowed" if a.allow_network else "I ask the first time"))
        if account:
            rows.append(("Usage", f"your ChatGPT plan · {USAGE_URL.removeprefix('https://')}"))
        segments: list[Segment] = []
        for label, value in rows:
            segments += [(f"{label:<17}", "dim"), (value, "bold"), ("\n", "")]
        yield self.text(Content.assemble(*segments[:-1]))
        yield self.text("You can change any of this later by running mentor init again.", "text dim")
        yield Line(id="error")

    def submit(self) -> None:
        app = self.setup
        try:
            apply_answers(app.paths, app.config, app.answers, app.store)
        except OSError as exc:
            self.error([("✗ Could not save: ", "$error"), (str(exc), "")])
            return
        app.saved = True
        self.app.push_screen(DoneStep())


class DoneStep(Step):
    heading = "You're all set"
    keys = "Enter start · Esc not now"

    def button_labels(self) -> tuple[str, str | None]:
        return ("Open my session", None) if self.setup.then_start else ("Start my first session", "Not now")

    def body(self) -> Iterator[Widget]:
        app = self.setup
        yield self.text(Content.assemble(("Saved to ", "dim"), (short_path(str(app.paths.config_file)), "")))
        if app.answers.provider == "mock":
            yield self.text(
                "You have no AI model yet, so I cannot make learning plans. "
                "Run mentor init again when you have an API key."
            )
        yield self.text(
            "Next: your first session. Your screen splits into three parts: me on the left, Neovim and "
            "the shell on the right. I will show you around, then ask what you want to learn."
        )

    def action_back(self) -> None:
        self.setup.finish(start=False)

    def submit(self) -> None:
        self.setup.finish(start=True)


# ---------------------------------------------------------------- the app
class SetupApp(App[SetupResult]):
    TITLE = "CLI Mentor setup"
    CSS = """
    Screen { align: center top; }
    #page { width: 100%; max-width: 90; height: 100%; padding: 1 2 0 2; }
    #top { height: 1; }
    #brand { width: 1fr; }
    #count { width: auto; }
    #rule { margin-bottom: 1; }
    #body { height: 1fr; scrollbar-size-vertical: 1; scrollbar-color: $mentor-rule; scrollbar-background: $background; }
    #heading { text-style: bold; color: $accent; margin-bottom: 1; }
    .text { margin-bottom: 1; }
    .tight { margin-bottom: 0; }
    .label { margin-top: 1; text-style: bold; }
    .dim { text-style: dim; }
    Input { border: round $border-blurred; background: $background; padding: 0 1; }
    Input:focus { border: round $accent; }
    OptionList, SelectionList {
        height: auto; max-height: 16; border: none; background: $background; padding: 0;
    }
    OptionList:focus, SelectionList:focus { border: none; background-tint: transparent; }
    Checkbox { border: none; background: $background; padding: 0; margin-top: 1; width: 1fr; height: auto; }
    Checkbox:focus { border: none; }
    #models > .option-list--option-highlighted { color: $accent; text-style: bold; background: $background; }
    #models:focus > .option-list--option-highlighted { color: $foreground; text-style: bold reverse; }
    #error { height: auto; margin-top: 1; }
    #loading { height: 1; }
    #buttons { height: auto; margin-top: 1; margin-bottom: 1; }
    #buttons Button {
        height: 1; min-width: 8; border: none; padding: 0 2; margin-right: 2;
        background: $background; color: $foreground; text-style: none;
    }
    #buttons #next { color: $accent; text-style: bold; }
    #buttons #back { text-style: dim; }
    #buttons Button:focus { text-style: bold reverse; }
    #buttons Button:hover { background: $background; text-style: underline; }
    #keys { height: 1; margin-bottom: 1; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [Binding("ctrl+c", "quit_setup", "Quit", priority=True)]

    def __init__(
        self,
        paths: AppPaths,
        config: Config,
        *,
        provider: ProviderName | None = None,
        model: str | None = None,
        workspace: str | None = None,
        base_url: str | None = None,
        fetch: FetchModels = fetch_models,
        then_start: bool = False,
        light: bool | None = None,
        auth: ChatGPTSignIn | None = None,
    ) -> None:
        super().__init__()
        self.paths = paths
        self.config = config
        self.auth: ChatGPTSignIn = auth or ChatGPTAuth(paths)
        self.store = open_store(paths.db_file)
        self.facts = proposed_facts(Profile(self.store))
        self.edits: dict[str, str] = {}
        chosen = provider or config.tutor.provider
        url = config.provider_settings(chosen).base_url if base_url is None else base_url
        # A PROVIDERS key. New setups start on the ChatGPT plan: no key to find, and the offline demo cannot teach.
        self.choice: str = "compatible" if chosen == "openai" and url else chosen
        if self.choice == "mock" and provider is None and not config.initialized:
            self.choice = chosen = "chatgpt"  # and suggest its model, not the offline demo's
        self.address = url or ""
        self.answers = SetupAnswers(
            workspace=str(Path(workspace or config.workspace_root).expanduser()),
            intervention=Profile(self.store).intervention_style(),
            provider=chosen,
            base_url=base_url,
            model=model or config.provider_settings(chosen).model,
        )
        self.fetch = fetch
        self.then_start = then_start
        self.saved = False
        self.register_theme(mentor_theme(terminal_is_light() if light is None else light))
        self.theme = "cli-mentor"

    def on_mount(self) -> None:
        self.push_screen(WelcomeStep())

    def finish(self, *, start: bool) -> None:
        self.exit(SetupResult(saved=self.saved, start=start and self.saved))

    def action_quit_setup(self) -> None:
        self.finish(start=False)


def run_setup(paths: AppPaths, config: Config, **options: object) -> SetupResult:
    """Run the full-screen setup and return what happened."""
    app = SetupApp(paths, config, **options)  # type: ignore[arg-type]
    try:
        return app.run() or SetupResult(saved=app.saved)
    finally:
        app.store.close()
