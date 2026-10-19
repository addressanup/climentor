"""CLI and onboarding tests (in-process, isolated folders, fake keyring)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from cli_mentor import cli
from cli_mentor.config import Config, load_config
from cli_mentor.db.store import open_store
from cli_mentor.memory.profile import Profile
from cli_mentor.memory.sessions import Sessions
from cli_mentor.onboarding import run_init
from cli_mentor.paths import AppPaths
from cli_mentor.permissions import Capability, PermissionManager


def answers(*items: str) -> Iterator[str]:
    yield from items


def test_interactive_init_confirms_edits_and_rejects(mentor_env: dict[str, str], tmp_path: Path) -> None:
    paths = AppPaths.from_env()
    ws = tmp_path / "practice"
    script = answers(
        "y",  # create practice folder
        "y",
        "e",
        "Wants to learn the CLI first, Neovim later.",
        "n",
        "",
        "",
        "",
        "",
        "",  # 8 profile facts
        "quiet",  # intervention style
        "I have used Excel a lot",  # own words
        "y",  # remember observation approval
        "mock",  # provider
    )
    run_init(paths, Config(), workspace=str(ws), ask=lambda _p: next(script), say=lambda _m: None)
    assert ws.is_dir() and oct(ws.stat().st_mode & 0o777) == "0o700"
    store = open_store(paths.db_file)
    prof = Profile(store)
    facts = {f.key: f for f in prof.facts(include_rejected=True)}
    assert facts["platform"].status == "confirmed"
    assert facts["goal.breadth"].value == "Wants to learn the CLI first, Neovim later."
    assert facts["goal.breadth"].provenance == "user_stated"
    assert facts["goal.cli_nvim"].status == "rejected"
    assert facts["goal.own_words"].value == "I have used Excel a lot"
    assert prof.intervention_style() == "quiet"
    assert PermissionManager(store, "any-session").check(Capability.OBSERVE_TERMINAL).allowed
    cfg = load_config(paths.config_file)
    assert cfg.initialized and cfg.tutor.provider == "mock" and cfg.workspace_root == str(ws)


def test_init_declining_folder_changes_nothing(mentor_env: dict[str, str], tmp_path: Path) -> None:
    paths = AppPaths.from_env()
    run_init(paths, Config(), workspace=str(tmp_path / "nope"), ask=lambda _p: "n", say=lambda _m: None)
    assert not (tmp_path / "nope").exists()
    assert not load_config(paths.config_file).initialized


def test_init_real_provider_stores_key_in_keychain_not_config(
    mentor_env: dict[str, str], tmp_path: Path, memory_keyring: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths.from_env()
    monkeypatch.setattr("getpass.getpass", lambda _p: "sk-proj-ABCDEFGHIJKLMNOPQRSTUV")
    script = answers("y", *[""] * 8, "", "", "n", "openai", "n", "my-model-id", "y")
    run_init(paths, Config(), workspace=str(tmp_path / "w"), ask=lambda _p: next(script), say=lambda _m: None)
    cfg_text = paths.config_file.read_text()
    assert "ABCDEFGHIJ" not in cfg_text
    assert memory_keyring.get_password("cli-mentor", "openai") == "sk-proj-ABCDEFGHIJKLMNOPQRSTUV"  # type: ignore[attr-defined]
    cfg = load_config(paths.config_file)
    assert cfg.tutor.provider == "openai" and cfg.providers.openai.model == "my-model-id"


def test_cli_commands(mentor_env: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["init", "--yes", "--provider", "mock", "--workspace", str(tmp_path / "w")]) == 0
    capsys.readouterr()
    assert cli.main(["doctor", "--json"]) in (0, 1)
    checks = json.loads(capsys.readouterr().out)
    assert {c["name"] for c in checks} >= {"Python", "tmux", "Neovim", "SQLite FTS5", "Setup"}
    assert cli.main(["profile"]) == 0
    assert "platform" in capsys.readouterr().out
    assert cli.main(["profile", "set", "goal.extra", "learn", "git"]) == 0
    assert cli.main(["memory", "add", "likes", "short", "answers"]) == 0
    assert cli.main(["memory", "search", "short"]) == 0
    assert "likes short answers" in capsys.readouterr().out
    paths = AppPaths.from_env()
    store = open_store(paths.db_file)
    sid = Sessions(store).create("mock", "mock-tutor")
    out_file = tmp_path / "export.json"
    assert cli.main(["export", "--out", str(out_file)]) == 0
    data = json.loads(out_file.read_text())
    assert data["format"] == "cli-mentor-export" and any(s["id"] == sid for s in data["sessions"])
    assert oct(out_file.stat().st_mode & 0o777) == "0o600"
    assert cli.main(["sessions"]) == 0
    assert sid in capsys.readouterr().out
    assert cli.main(["delete-session", sid, "--yes"]) == 0
    assert Sessions(open_store(paths.db_file)).get(sid) is None
    assert cli.main(["key", "status", "openai"]) == 0
    assert "not found" in capsys.readouterr().out


def test_start_requires_init(mentor_env: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["start", "--detach"]) == 1
    assert "mentor init" in capsys.readouterr().out


def test_commands_doc_matches_in_app_help() -> None:
    import re

    from cli_mentor.tutor.commands import HELP

    doc = (Path(__file__).resolve().parents[2] / "docs" / "COMMANDS.md").read_text()
    for cmd in sorted(set(re.findall(r"`(/[a-z]+)", HELP))):
        assert f"`{cmd}" in doc, f"{cmd} missing from docs/COMMANDS.md"
    parser_cmds = {
        "doctor",
        "init",
        "start",
        "resume",
        "stop",
        "sessions",
        "subjects",
        "profile",
        "memory",
        "export",
        "delete-session",
        "key",
        "demo",
    }
    for name in parser_cmds:
        assert f"`mentor {name}" in doc, f"mentor {name} missing from docs/COMMANDS.md"


def test_init_with_base_url_contacts_that_server(
    mentor_env: dict[str, str], tmp_path: Path, memory_keyring: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx2

    from cli_mentor.providers.openai_provider import OpenAIProvider

    memory_keyring.set_password("cli-mentor", "openai", "sk-test-deepseek-0123456789")  # type: ignore[attr-defined]
    seen: list[str] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.append(str(req.url))
        return httpx2.Response(401, json={"error": {"message": "invalid key"}})

    def fake_build(config: Config, provider: str) -> OpenAIProvider:
        return OpenAIProvider(
            "sk-test-deepseek-0123456789",
            config.provider_settings(provider),  # type: ignore[arg-type]
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        )

    monkeypatch.setattr("cli_mentor.onboarding.build_provider", fake_build)
    said: list[str] = []
    prompts: list[str] = []
    script = answers("y", *[""] * 8, "", "", "n", "y", "deepseek-v4-pro", "y")

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return next(script)

    paths = AppPaths.from_env()
    run_init(
        paths,
        Config(),
        workspace=str(tmp_path / "w"),
        provider="openai",
        base_url="https://api.deepseek.com",
        ask=ask,
        say=said.append,
    )
    assert seen and seen[0].startswith("https://api.deepseek.com/")
    assert any("from https://api.deepseek.com?" in p for p in prompts)
    assert any("The server at https://api.deepseek.com rejected the API key" in s for s in said)
    assert "sk-test-deepseek" not in "\n".join(said + prompts)
    cfg = load_config(paths.config_file)
    assert cfg.providers.openai.base_url == "https://api.deepseek.com"
    assert cfg.providers.openai.model == "deepseek-v4-pro"


def test_choose_subject_picker() -> None:
    subjects = [("C++ from the very basics", "3/40 steps"), ("Rust", "1/12 steps")]
    said: list[str] = []
    assert cli.choose_subject(subjects, ask=lambda _p: "", say=said.append) == 0  # Enter = most recent
    assert "   1  C++ from the very basics  3/40 steps" in said
    assert "   2  Rust                      1/12 steps" in said
    assert "   n  Start a new subject\n" in said
    assert cli.choose_subject(subjects, ask=lambda _p: "2", say=said.append) == 1
    assert cli.choose_subject(subjects, ask=lambda _p: "n", say=said.append) is None
    assert cli.choose_subject(subjects, ask=lambda _p: "9", say=said.append) is None


def test_subjects_command(mentor_env: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from cli_mentor.curriculum.plans import PlanOutline, PlanStore
    from tests.plan_helpers import fixture

    assert cli.main(["init", "--yes", "--provider", "mock", "--workspace", str(tmp_path / "w")]) == 0
    assert cli.main(["subjects"]) == 0
    assert "No subjects yet" in capsys.readouterr().out
    store = open_store(AppPaths.from_env().db_file)
    plans = PlanStore(store)
    plans.activate(plans.create("learn C++", PlanOutline.model_validate(fixture()["plan"])).id)
    assert cli.main(["subjects"]) == 0
    out = capsys.readouterr().out
    assert "   1  C++ from the very basics  ──────────  0/3 steps · studied just now" in out
    assert str(tmp_path / "w" / "c-from-the-very-basics") in out
    assert cli.main(["start", "--detach", "--subject", "nope"]) == 1


def test_relative_times_and_progress_bar() -> None:
    now = 1_000_000.0
    assert cli._ago(now - 30, now) == "just now" and cli._ago(now - 600, now) == "10 minutes ago"
    assert cli._ago(now - 3700, now) == "1 hour ago" and cli._ago(now - 90_000, now) == "yesterday"
    assert cli._ago(now - 5 * 86400, now) == "5 days ago" and cli._ago(None) == "never"
    assert cli._bar(3, 10) == "━━━───────" and cli._bar(0, 0) == "──────────"
