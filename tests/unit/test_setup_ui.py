"""Full-screen setup tests, driven with Textual's Pilot (temp folders, fake keyring, fake model list)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

from textual.screen import Screen
from textual.widgets import Button, Input

from cli_mentor.chatgpt_auth import PLAN_SCOPE, Account, SignInError
from cli_mentor.config import Config, ProviderName, load_config
from cli_mentor.db.store import open_store
from cli_mentor.memory.profile import Profile
from cli_mentor.paths import AppPaths
from cli_mentor.permissions import Capability, PermissionManager
from cli_mentor.providers.base import ProviderError
from cli_mentor.ui.setup import (
    AboutStep,
    DoneStep,
    EditScreen,
    FolderStep,
    ModelPickStep,
    ModelStep,
    ReviewStep,
    SetupApp,
    SetupResult,
    SignInStep,
    StyleStep,
    describe_folder,
)


async def at(pilot, screen: type[Screen[None]] | type[EditScreen]) -> None:  # type: ignore[no-untyped-def]
    for _ in range(60):
        if isinstance(pilot.app.screen, screen):
            await pilot.pause(0.05)
            return
        await pilot.pause(0.05)
    raise AssertionError(f"never reached {screen.__name__}; on {type(pilot.app.screen).__name__}")


def type_into(app: SetupApp, selector: str, text: str) -> None:
    app.screen.query_one(selector, Input).value = text


async def test_setup_walks_through_and_saves(
    mentor_env: dict[str, str], tmp_path: Path, memory_keyring: object
) -> None:
    paths = AppPaths.from_env()
    paths.ensure()
    ws = tmp_path / "practice"
    asked: list[tuple[str, str]] = []

    async def fetch(config: Config, provider: ProviderName) -> list[str]:
        asked.append((provider, config.provider_settings(provider).base_url))
        return ["deepseek-chat", "deepseek-reasoner"]

    app = SetupApp(paths, Config(workspace_root=str(ws)), fetch=fetch)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause(0.1)
        await pilot.press("enter")  # welcome: Begin
        await at(pilot, FolderStep)
        await pilot.press("enter")  # the suggested folder is fine
        await at(pilot, AboutStep)
        await pilot.press("space")  # remove the first guess
        await pilot.press("down", "e")  # change the words of the second
        await at(pilot, EditScreen)
        type_into(app, "#edit", "Wants to learn C++ properly.")
        await pilot.press("enter")
        await at(pilot, AboutStep)
        await pilot.press("enter")
        await at(pilot, StyleStep)
        await pilot.press("down", "enter")  # quiet; Enter moves to the next field
        await pilot.press("space", "enter")  # remember observation approval
        type_into(app, "#own", "I know some Python")
        await pilot.press("enter")
        await at(pilot, ModelStep)
        await pilot.press("down", "down", "down", "enter")  # “another service that works like OpenAI”
        type_into(app, "#address", "https://api.deepseek.com/")
        await pilot.press("enter")
        type_into(app, "#key", "sk-test-deepseek-0123456789")
        await pilot.press("enter", "space", "enter")  # allow sending, then continue
        await at(pilot, ModelPickStep)
        await pilot.pause(0.2)
        await pilot.press("down", "down", "enter")  # into the list, second model
        await at(pilot, ReviewStep)
        assert app.answers.model == "deepseek-reasoner"
        review = "\n".join(str(w.render()) for w in app.screen.query(".text"))
        assert "deepseek-reasoner at https://api.deepseek.com" in review and "1 removed" in review
        await pilot.press("enter")  # Save
        await at(pilot, DoneStep)
        await pilot.press("enter")  # Start my first session
    assert app.return_value == SetupResult(saved=True, start=True)
    assert asked == [("openai", "https://api.deepseek.com")]
    assert ws.is_dir() and oct(ws.stat().st_mode & 0o777) == "0o700"
    cfg = load_config(paths.config_file)
    assert cfg.initialized and cfg.tutor.provider == "openai" and cfg.workspace_root == str(ws)
    assert cfg.providers.openai.base_url == "https://api.deepseek.com"
    assert cfg.providers.openai.model == "deepseek-reasoner"
    assert "sk-test" not in paths.config_file.read_text()
    assert memory_keyring.get_password("cli-mentor", "openai") == "sk-test-deepseek-0123456789"  # type: ignore[attr-defined]
    store = open_store(paths.db_file)
    facts = {f.key: f for f in Profile(store).facts(include_rejected=True)}
    assert facts["platform"].status == "rejected"
    assert (
        facts["goal.breadth"].value == "Wants to learn C++ properly."
        and facts["goal.breadth"].provenance == "user_stated"
    )
    assert facts["goal.cli_nvim"].status == "confirmed"
    assert facts["goal.own_words"].value == "I know some Python"
    assert Profile(store).intervention_style() == "quiet"
    pm = PermissionManager(store, "any-session")
    assert pm.check(Capability.OBSERVE_TERMINAL).allowed and pm.check(Capability.NETWORK, "openai").allowed


async def test_setup_quit_saves_nothing(mentor_env: dict[str, str], tmp_path: Path) -> None:
    paths = AppPaths.from_env()
    paths.ensure()
    app = SetupApp(paths, Config(workspace_root=str(tmp_path / "w")))
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.press("enter")
        await at(pilot, FolderStep)
        await pilot.press("ctrl+c")
    assert app.return_value == SetupResult(saved=False)
    assert not paths.config_file.exists() and not (tmp_path / "w").exists()


async def test_setup_rejects_a_file_as_folder_and_esc_goes_back(mentor_env: dict[str, str], tmp_path: Path) -> None:
    paths = AppPaths.from_env()
    paths.ensure()
    afile = tmp_path / "notes.txt"
    afile.write_text("x")
    assert describe_folder(str(afile))[0] is False and describe_folder("")[0] is False
    app = SetupApp(paths, Config(workspace_root=str(afile)))
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.press("enter")
        await at(pilot, FolderStep)
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert isinstance(app.screen, FolderStep)
        assert "file, not a folder" in str(app.screen.query_one("#error").render())
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert type(app.screen).__name__ == "WelcomeStep"


async def test_setup_model_list_error_lets_you_type_an_id(mentor_env: dict[str, str], tmp_path: Path) -> None:
    paths = AppPaths.from_env()
    paths.ensure()

    async def failing(config: Config, provider: ProviderName) -> list[str]:
        raise ProviderError("auth", "The server at api.anthropic.com rejected the API key.")

    app = SetupApp(paths, Config(workspace_root=str(tmp_path / "w")), fetch=failing)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.press("enter")
        await at(pilot, FolderStep)
        await pilot.press("enter")
        await at(pilot, AboutStep)
        await pilot.press("enter")
        await at(pilot, StyleStep)
        await pilot.press("enter", "enter", "enter")
        await at(pilot, ModelStep)  # the ChatGPT plan is chosen by default for a new setup
        await pilot.press("down", "enter")  # Anthropic, then on to the key field
        await pilot.press("enter", "enter")  # no key: an error, and we stay
        await pilot.pause(0.1)
        assert isinstance(app.screen, ModelStep)
        assert "Paste your API key" in str(app.screen.query_one("#error").render())
        type_into(app, "#key", "sk-ant-test-0123456789")
        await pilot.press("enter", "enter")
        await at(pilot, ModelPickStep)
        await pilot.pause(0.2)
        assert "rejected the API key" in str(app.screen.query_one("#error").render())
        type_into(app, "#model", "claude-test-model")
        await pilot.press("enter")
        await at(pilot, ReviewStep)
        assert app.answers.provider == "anthropic" and app.answers.model == "claude-test-model"


# ---------------------------------------------------------------- Continue with ChatGPT
def plan_account() -> Account:
    return Account(
        "oaiapp_test",
        "user-1",
        "learner@example.com",
        scopes=[PLAN_SCOPE],
        access_token="access",
        refresh_token="refresh",
        expires_at=time.time() + 3600,
    )


class FakeSignIn:
    """Stands in for ChatGPTAuth: no browser, no network."""

    def __init__(self, result: Account | None = None, error: SignInError | None = None) -> None:
        self.saved: Account | None = None
        self.result, self.error = result, error
        self.calls = 0
        self.opened: list[str] = []
        self.open_browser: Callable[[str], object] = self.opened.append

    def account(self) -> Account | None:
        return self.saved

    async def sign_in(self, on_url: Callable[[str], None] | None = None, *, timeout: float = 600.0) -> Account:
        self.calls += 1
        if on_url:
            on_url("https://auth.openai.com/api/accounts/authorize?client_id=dynamic_agent_client")
        await asyncio.sleep(0.05)
        if self.error:
            raise self.error
        assert self.result is not None
        self.saved = self.result
        return self.result


async def to_model_step(pilot) -> None:  # type: ignore[no-untyped-def]
    await pilot.press("enter")
    await at(pilot, FolderStep)
    await pilot.press("enter")
    await at(pilot, AboutStep)
    await pilot.press("enter")
    await at(pilot, StyleStep)
    await pilot.press("enter", "enter", "enter")
    await at(pilot, ModelStep)


async def settled(pilot, step: SignInStep) -> None:  # type: ignore[no-untyped-def]
    for _ in range(60):
        if not step.waiting:
            return
        await pilot.pause(0.05)
    raise AssertionError("the sign-in never finished")


async def test_setup_continues_with_chatgpt_and_saves(mentor_env: dict[str, str], tmp_path: Path) -> None:
    paths = AppPaths.from_env()
    paths.ensure()
    auth = FakeSignIn(result=plan_account())
    asked: list[str] = []

    async def fetch(config: Config, provider: ProviderName) -> list[str]:
        asked.append(provider)
        return ["gpt-b", "gpt-a"]

    app = SetupApp(paths, Config(workspace_root=str(tmp_path / "w")), fetch=fetch, auth=auth)
    async with app.run_test(size=(100, 40)) as pilot:
        await to_model_step(pilot)
        assert str(app.screen.query_one("#next", Button).label) == "Continue with ChatGPT"
        assert not app.screen.query_one("#key-box").display  # no API key needed
        await pilot.press("enter")  # ChatGPT plan, then on to the “send” tick
        await pilot.press("space", "enter")  # allow sending, continue
        await at(pilot, SignInStep)
        step = app.screen
        assert isinstance(step, SignInStep)
        await settled(pilot, step)
        assert "You're using your ChatGPT plan" in str(step.query_one("#heading").render())
        assert str(step.query_one("#next", Button).label) == "Got it"
        await pilot.press("enter")
        await at(pilot, ModelPickStep)
        await pilot.pause(0.2)
        await pilot.press("down", "down", "enter")  # the second model
        await at(pilot, ReviewStep)
        review = "\n".join(str(w.render()) for w in app.screen.query(".text"))
        assert "ChatGPT plan · gpt-a (learner@example.com)" in review and "chatgpt.com/settings/usage" in review
        await pilot.press("enter")
        await at(pilot, DoneStep)
        await pilot.press("escape")
    assert auth.calls == 1 and asked == ["chatgpt"]
    cfg = load_config(paths.config_file)
    assert cfg.tutor.provider == "chatgpt" and cfg.providers.chatgpt.model == "gpt-a"
    assert PermissionManager(open_store(paths.db_file), "s").check(Capability.NETWORK, "chatgpt").allowed


async def test_setup_skips_sign_in_when_already_signed_in(mentor_env: dict[str, str], tmp_path: Path) -> None:
    paths = AppPaths.from_env()
    paths.ensure()
    auth = FakeSignIn()
    auth.saved = plan_account()

    async def fetch(config: Config, provider: ProviderName) -> list[str]:
        return ["gpt-a"]

    app = SetupApp(paths, Config(workspace_root=str(tmp_path / "w")), fetch=fetch, auth=auth)
    async with app.run_test(size=(100, 40)) as pilot:
        await to_model_step(pilot)
        assert "Signed in as learner@example.com" in str(app.screen.query_one("#account").render())
        assert str(app.screen.query_one("#next", Button).label) == "Continue"
        await pilot.press("enter", "enter")
        await at(pilot, ModelPickStep)
    assert auth.calls == 0


async def test_setup_sign_in_declined_can_try_again(mentor_env: dict[str, str], tmp_path: Path) -> None:
    paths = AppPaths.from_env()
    paths.ensure()
    auth = FakeSignIn(error=SignInError("access_denied", "You cancelled the sign-in or did not allow it."))
    app = SetupApp(paths, Config(workspace_root=str(tmp_path / "w")), auth=auth)
    async with app.run_test(size=(100, 40)) as pilot:
        await to_model_step(pilot)
        await pilot.press("enter", "enter")
        await at(pilot, SignInStep)
        step = app.screen
        assert isinstance(step, SignInStep)
        await settled(pilot, step)
        assert "cancelled the sign-in" in str(step.query_one("#error").render())
        assert str(step.query_one("#next", Button).label) == "Try again"
        await pilot.press("o")
        assert auth.opened and auth.opened[0].startswith("https://auth.openai.com/")
        await pilot.press("enter")  # try again
        await pilot.pause(0.05)
        await settled(pilot, step)
        await pilot.press("escape")
        await at(pilot, ModelStep)
    assert auth.calls == 2
