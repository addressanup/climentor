"""Full-screen setup tests, driven with Textual's Pilot (temp folders, fake keyring, fake model list)."""

from __future__ import annotations

from pathlib import Path

from textual.screen import Screen
from textual.widgets import Input

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
        await pilot.press("down", "down", "enter")  # “another service that works like OpenAI”
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
        await at(pilot, ModelStep)  # Anthropic is chosen by default for a new setup
        await pilot.press("enter")  # on to the key field
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
