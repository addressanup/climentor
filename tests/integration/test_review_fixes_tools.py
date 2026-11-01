"""Review fixes that need the real tools: tmux (API keys in new windows) and Neovim (a space in the path)."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from cli_mentor import lifecycle
from cli_mentor.paths import AppPaths
from tests.conftest import require_tool
from tests.helpers import private_tmux

pytestmark = pytest.mark.integration

FAKE_KEY = "sk-ant-FAKE-for-test-000000000000"


def wait(predicate: Callable[[], object], what: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.05)


def written(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


def test_api_keys_stay_out_of_new_windows_but_reach_a_restarted_tutor(
    mentor_env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_tool("tmux")
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)  # the learner exported it before `mentor start`
    tmux = private_tmux()
    monkeypatch.setattr(lifecycle, "make_tmux", lambda _paths: tmux)
    paths = AppPaths.from_env()
    sid = "20261101-090740-abcdef"
    name = lifecycle.tmux_session_name(sid)
    first, window, again = (tmp_path / n for n in ("first", "window", "again"))
    try:
        tmux.run("new-session", "-d", "-s", name, f"env > {shlex.quote(str(first))}; sleep 1")
        lifecycle._hide_secrets(tmux, name)  # what create_workspace does right after creating the session
        tutor = tmux.panes(name)[0].pane_id
        lifecycle._set_role(tmux, tutor, "tutor")
        tmux.run("set-option", "-p", "-t", tutor, "remain-on-exit", "on")
        tmux.run("new-window", "-t", f"={name}:", f"env > {shlex.quote(str(window))}; sleep 30")
        wait(lambda: written(first) and written(window), "both panes to write their environment")
        assert FAKE_KEY in first.read_text()  # the tutor needs it
        assert "ANTHROPIC_API_KEY" not in window.read_text()  # anything the learner runs here must not get it
        wait(lambda: any(p.dead for p in tmux.panes(name) if p.role == "tutor"), "the tutor pane to stop")
        monkeypatch.setattr(lifecycle, "tutor_command", lambda _sid: f"env > {shlex.quote(str(again))}; sleep 30")
        assert lifecycle.repair_tutor(paths, sid)
        wait(lambda: written(again), "the restarted tutor to write its environment")
        assert FAKE_KEY in again.read_text()
        later = tmp_path / "later"
        tmux.run("new-window", "-t", f"={name}:", f"env > {shlex.quote(str(later))}; sleep 30")
        wait(lambda: written(later), "a window opened after the restart")
        assert "ANTHROPIC_API_KEY" not in later.read_text()
    finally:
        tmux.kill_server()


def test_editor_command_loads_the_plugin_from_a_folder_with_a_space(tmp_path: Path) -> None:
    nvim = require_tool("nvim")
    spaced = tmp_path / "My Projects" / "nvim"
    shutil.copytree(lifecycle.plugin_dir(), spaced)
    probe = "lua io.stdout:write('loaded=' .. tostring(package.loaded['cli_mentor'] ~= nil))"
    command = f"{lifecycle.editor_command(nvim)} --headless --clean -c {shlex.quote(probe)} -c qa!"
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLI_MENTOR_")}
    env["CLI_MENTOR_NVIM_RTP"] = str(spaced)  # no socket: the plugin loads, then start() does nothing
    proc = subprocess.run(["sh", "-c", command], env=env, capture_output=True, text=True, timeout=30)
    assert "E518" not in proc.stdout + proc.stderr
    assert "loaded=true" in proc.stdout, proc.stdout + proc.stderr
