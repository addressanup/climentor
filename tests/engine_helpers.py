"""Build a headless Engine for tests (real SQLite, real IPC socket, mock provider)."""

from __future__ import annotations

import asyncio
import itertools
import time
from pathlib import Path
from typing import Any

from cli_mentor.config import Config
from cli_mentor.db.store import open_store
from cli_mentor.events.schema import SourceKind
from cli_mentor.observe.ipc import ClientIdentity
from cli_mentor.paths import AppPaths
from cli_mentor.providers.base import Provider
from cli_mentor.providers.mock import MockProvider
from cli_mentor.tmuxutil import Tmux
from cli_mentor.tutor.engine import Engine
from cli_mentor.tutor.ui_protocol import HeadlessUi

_conn = itertools.count(1)


def make_config(workspace: Path, **tutor: Any) -> Config:
    cfg = Config(initialized=True, workspace_root=str(workspace))
    for key, value in tutor.items():
        setattr(cfg.tutor, key, value)
    cfg.tutor.debounce_seconds = tutor.get("debounce_seconds", 0.0)
    cfg.tutor.unsolicited_cooldown_seconds = tutor.get("unsolicited_cooldown_seconds", 0.0)
    return cfg


def make_engine(
    workspace: Path,
    *,
    provider: Provider | None = None,
    ui: HeadlessUi | None = None,
    config: Config | None = None,
    session_id: str | None = None,
    tmux: Tmux | None = None,
    tmux_session: str | None = None,
    tutor_pane: str | None = None,
) -> Engine:
    paths = AppPaths.from_env()
    paths.ensure()
    store = open_store(paths.db_file)
    from cli_mentor.memory.sessions import Sessions

    cfg = config or make_config(workspace)
    sid = session_id or Sessions(store).create("mock", "mock-tutor")
    return Engine(
        config=cfg,
        paths=paths,
        store=store,
        session_id=sid,
        provider=provider or MockProvider(),
        ui=ui or HeadlessUi(),
        tmux=tmux,
        tmux_session=tmux_session,
        tutor_pane=tutor_pane,
    )


class FakeShell:
    """Sends shell events into the engine as if the zsh hooks had sent them."""

    def __init__(self, engine: Engine, sid: str = "zsh-1") -> None:
        self.engine = engine
        self.ident = ClientIdentity(SourceKind.SHELL, sid, "e1", next(_conn), None)
        self.seq = 0

    def send(self, payload: dict[str, Any]) -> None:
        self.seq += 1
        self.engine._on_ipc_message(self.ident, {"seq": self.seq, "ts": time.time(), **payload})

    def run(self, command: str, cwd: Path | str, status: int = 0) -> None:
        now = time.time()
        self.send({"type": "shell.command_started", "command": command, "cwd": str(cwd), "started_at": now})
        self.send(
            {
                "type": "shell.command_finished",
                "command": command,
                "cwd": str(cwd),
                "exit_status": status,
                "started_at": now,
                "finished_at": now + 0.01,
            }
        )


class FakeNvim:
    def __init__(self, engine: Engine, sid: str = "nvim-1") -> None:
        self.engine = engine
        self.ident = ClientIdentity(SourceKind.NVIM, sid, "e1", next(_conn), None)
        self.seq = 0

    def send(self, payload: dict[str, Any]) -> None:
        self.seq += 1
        self.engine._on_ipc_message(self.ident, {"seq": self.seq, "ts": time.time(), **payload})


async def settle(engine: Engine, rounds: int = 5) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0.02)
        if engine.queue.empty():
            await asyncio.sleep(0.02)
            return
