"""Small synchronous wrapper around the tmux command line.

Every call targets one named tmux server (``tmux -L <name>``), never the
learner's default server.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

DEFAULT_SOCKET = "cli-mentor"

PANE_FORMAT = (
    "#{pane_id}\t#{pane_index}\t#{pane_pid}\t#{pane_dead}\t#{pane_current_command}\t"
    "#{pane_current_path}\t#{pane_width}\t#{pane_height}\t#{@mentor_role}\t#{session_name}"
)


class TmuxError(RuntimeError):
    pass


@dataclass(frozen=True)
class PaneInfo:
    pane_id: str
    index: int
    pid: int
    dead: bool
    current_command: str
    current_path: str
    width: int
    height: int
    role: str
    session: str


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    if extra:
        env.update(extra)
    return env


class Tmux:
    def __init__(
        self,
        socket_name: str = DEFAULT_SOCKET,
        config_file: str | None = None,
        tmux_bin: str | None = None,
    ) -> None:
        self.socket_name = socket_name
        self.config_file = config_file
        self.bin = tmux_bin or shutil.which("tmux") or "tmux"

    def argv(self, *args: str) -> list[str]:
        base = [self.bin, "-L", self.socket_name]
        if self.config_file:
            base += ["-f", self.config_file]
        return [*base, *args]

    def run(self, *args: str, check: bool = True, timeout: float = 10.0) -> str:
        proc = subprocess.run(
            self.argv(*args),
            capture_output=True,
            text=True,
            env=clean_env(),
            timeout=timeout,
        )
        if check and proc.returncode != 0:
            raise TmuxError(f"tmux {' '.join(args[:2])} failed: {proc.stderr.strip()}")
        return proc.stdout

    def server_running(self) -> bool:
        proc = subprocess.run(
            self.argv("list-sessions"), capture_output=True, text=True, env=clean_env()
        )
        return proc.returncode == 0

    def sessions(self) -> list[str]:
        if not self.server_running():
            return []
        out = self.run("list-sessions", "-F", "#{session_name}", check=False)
        return [s for s in out.splitlines() if s]

    def has_session(self, name: str) -> bool:
        proc = subprocess.run(
            self.argv("has-session", "-t", f"={name}"),
            capture_output=True,
            text=True,
            env=clean_env(),
        )
        return proc.returncode == 0

    def panes(self, target: str) -> list[PaneInfo]:
        out = self.run("list-panes", "-s", "-t", f"={target}", "-F", PANE_FORMAT)
        panes: list[PaneInfo] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 10:
                continue
            panes.append(
                PaneInfo(
                    pane_id=parts[0],
                    index=int(parts[1]),
                    pid=int(parts[2] or 0),
                    dead=parts[3] == "1",
                    current_command=parts[4],
                    current_path=parts[5],
                    width=int(parts[6]),
                    height=int(parts[7]),
                    role=parts[8],
                    session=parts[9],
                )
            )
        return panes

    def capture_pane(self, pane_id: str, history: int = 200) -> list[str]:
        out = self.run("capture-pane", "-p", "-J", "-t", pane_id, "-S", f"-{history}")
        lines = out.split("\n")
        while lines and not lines[-1].strip():
            lines.pop()
        return lines

    def send_keys(self, pane_id: str, *keys: str, literal: bool = False) -> None:
        args = ["send-keys", "-t", pane_id]
        if literal:
            args.append("-l")
        self.run(*args, *keys)

    def kill_session(self, name: str) -> None:
        self.run("kill-session", "-t", f"={name}", check=False)

    def kill_server(self) -> None:
        self.run("kill-server", check=False)
