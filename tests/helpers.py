"""Helpers shared by integration and end-to-end tests."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from cli_mentor.tmuxutil import Tmux


def unique_socket() -> str:
    return f"cmtest-{uuid.uuid4().hex[:10]}"


def private_tmux() -> Tmux:
    return Tmux(unique_socket(), config_file="/dev/null")


async def wait_for(predicate: Callable[[], object], timeout: float = 10.0, interval: float = 0.05) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(interval)
    raise AssertionError("condition not met within timeout")


def make_home(tmp: Path) -> Path:
    """A fake HOME with a minimal .zshrc, so tests never load the real one."""
    home = tmp / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".zshrc").write_text("PROMPT='test> '\nunsetopt BEEP\n")
    return home
