"""Real zsh in a private tmux server, talking to the real IPC server."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from cli_mentor.events.schema import SourceKind
from cli_mentor.observe.ipc import ClientIdentity, IpcServer, new_token
from cli_mentor.observe.shell import prepare_zdotdir, shell_environment
from cli_mentor.paths import ensure_private_dir, write_private_file
from tests.conftest import require_tool
from tests.helpers import make_home, private_tmux, wait_for

pytestmark = pytest.mark.integration


class Collector:
    def __init__(self) -> None:
        self.messages: list[tuple[ClientIdentity, dict[str, Any]]] = []

    def __call__(self, ident: ClientIdentity, msg: dict[str, Any]) -> None:
        self.messages.append((ident, msg))

    def of_type(self, t: str) -> list[dict[str, Any]]:
        return [m for _i, m in self.messages if m.get("type") == t]


async def _open(path: Path) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_unix_connection(str(path))


async def test_ipc_auth_and_permissions(short_tmp: Path) -> None:
    run = ensure_private_dir(short_tmp / "run")
    sock = run / "ipc.sock"
    token = new_token()
    got = Collector()
    server = IpcServer(sock, token, got)
    await server.start()
    try:
        assert stat.S_IMODE(sock.stat().st_mode) == 0o600
        assert stat.S_IMODE(run.stat().st_mode) == 0o700

        # Wrong token: rejected, message never delivered.
        r, w = await _open(sock)
        w.write(b'{"v":1,"token":"' + b"0" * 64 + b'","kind":"shell","id":"zsh-1","epoch":"e"}\n')
        w.write(b'{"seq":1,"type":"shell.command_finished"}\n')
        await w.drain()
        assert await r.read() == b""  # server closed the connection
        # No hello at all / garbage.
        r, w = await _open(sock)
        w.write(b"not json\n")
        await w.drain()
        assert await r.read() == b""
        # Kind not allowed for clients.
        r, w = await _open(sock)
        w.write(
            json.dumps(
                {"v": 1, "token": token, "kind": "verifier", "id": "x", "epoch": "e"}
            ).encode()
            + b"\n"
        )
        await w.drain()
        assert await r.read() == b""
        assert server.rejected == 3
        assert got.messages == []

        # Correct token: delivered with identity from the hello.
        r, w = await _open(sock)
        w.write(
            json.dumps(
                {"v": 1, "token": token, "kind": "nvim", "id": "nvim-9", "epoch": "e1"}
            ).encode()
            + b"\n"
        )
        await w.drain()
        reply = json.loads(await r.readline())
        assert reply["ok"] is True
        w.write(b'{"seq":1,"type":"nvim.mode","mode":"i"}\n')
        await w.drain()
        await wait_for(lambda: got.messages)
        ident, _msg = got.messages[0]
        assert ident.kind == SourceKind.NVIM and ident.id == "nvim-9"
        assert ident.peer_uid == os.getuid()
        # Control messages reach connected Neovim clients.
        assert await server.send(SourceKind.NVIM, {"cmd": "pause"}) == 1
        assert json.loads(await r.readline()) == {"cmd": "pause"}
        w.close()
    finally:
        await server.stop()
    assert not sock.exists()


async def test_stale_socket_is_replaced_but_live_one_is_not(short_tmp: Path) -> None:
    run = ensure_private_dir(short_tmp / "run")
    sock = run / "ipc.sock"
    first = IpcServer(sock, new_token(), Collector())
    await first.start()
    second = IpcServer(sock, new_token(), Collector())
    with pytest.raises(RuntimeError):
        await second.start()
    await first.stop()
    # Leave a dead socket file behind and check it is cleaned up.
    import socket as pysocket

    s = pysocket.socket(pysocket.AF_UNIX)
    s.bind(str(sock))
    s.close()
    assert sock.exists()
    third = IpcServer(sock, new_token(), Collector())
    await third.start()
    await third.stop()


async def test_zsh_hooks_report_commands(short_tmp: Path, tmp_path: Path) -> None:
    require_tool("tmux")
    zsh = require_tool("zsh")
    run = ensure_private_dir(short_tmp / "run")
    sock = run / "ipc.sock"
    token = new_token()
    token_file = run / "token"
    write_private_file(token_file, token)
    got = Collector()
    server = IpcServer(sock, token, got)
    await server.start()
    tmux = private_tmux()
    home = make_home(tmp_path)
    zdot = prepare_zdotdir(run)
    env = shell_environment(
        zdotdir=zdot, socket_path=sock, token_file=token_file, nvim_rtp=None, user_zdotdir=None
    )
    env["HOME"] = str(home)
    env_args: list[str] = []
    for key, value in env.items():
        env_args += ["-e", f"{key}={value}"]
    try:
        tmux.run("new-session", "-d", "-s", "t", "-x", "120", "-y", "40", *env_args, f"{zsh} -l")
        pane = tmux.panes("t")[0].pane_id
        await wait_for(lambda: got.of_type("shell.session_started"), timeout=10)
        started = got.of_type("shell.session_started")[0]
        assert started["pane"] == pane

        tmux.send_keys(pane, "cd /tmp && false", "Enter")
        await wait_for(lambda: got.of_type("shell.command_finished"))
        fin = got.of_type("shell.command_finished")[0]
        assert fin["command"] == "cd /tmp && false"
        assert fin["exit_status"] == 1
        assert fin["cwd"] == "/tmp"
        assert fin["finished_at"] >= fin["started_at"]

        # Leading space: not recorded.
        tmux.send_keys(pane, " echo hidden-command", "Enter")
        # Quotes, backslashes, unicode survive JSON escaping.
        tricky = 'echo "a \\"quoted\\" \\\\ path" café $HOME'
        tmux.send_keys(pane, tricky, literal=True)
        tmux.send_keys(pane, "Enter")
        await wait_for(lambda: len(got.of_type("shell.command_finished")) >= 2)
        commands = [m["command"] for m in got.of_type("shell.command_finished")]
        assert all("hidden-command" not in c for c in commands)
        assert commands[1] == tricky
        # Sequence numbers increase; the skipped command did not consume one.
        seqs = [m["seq"] for _i, m in got.messages]
        assert seqs == sorted(seqs)
        # The token is not in the shell's environment.
        tmux.send_keys(
            pane, "env | grep -c CLI_MENTOR_TOKEN > " + str(tmp_path / "envcheck.txt"), "Enter"
        )
        await wait_for(
            lambda: (
                (tmp_path / "envcheck.txt").exists()
                and (tmp_path / "envcheck.txt").read_text().strip()
            )
        )
        assert (tmp_path / "envcheck.txt").read_text().strip() == "0"
        # The learner's normal ZDOTDIR behaviour is restored for child shells.
        tmux.send_keys(pane, "print -r -- ${ZDOTDIR:-unset} > " + str(tmp_path / "zd.txt"), "Enter")
        await wait_for(
            lambda: (tmp_path / "zd.txt").exists() and (tmp_path / "zd.txt").read_text().strip()
        )
        assert (tmp_path / "zd.txt").read_text().strip() == "unset"
    finally:
        tmux.kill_server()
        await server.stop()


async def test_zsh_hooks_survive_missing_server(short_tmp: Path, tmp_path: Path) -> None:
    """If the tutor is down, the shell keeps working and later events show a gap."""
    require_tool("tmux")
    zsh = require_tool("zsh")
    run = ensure_private_dir(short_tmp / "run")
    sock = run / "ipc.sock"
    token = new_token()
    token_file = run / "token"
    write_private_file(token_file, token)
    tmux = private_tmux()
    home = make_home(tmp_path)
    zdot = prepare_zdotdir(run)
    env = shell_environment(
        zdotdir=zdot, socket_path=sock, token_file=token_file, nvim_rtp=None, user_zdotdir=None
    )
    env["HOME"] = str(home)
    env_args = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
    got = Collector()
    server = IpcServer(sock, token, got)
    try:
        tmux.run("new-session", "-d", "-s", "t", "-x", "100", "-y", "30", *env_args, f"{zsh} -l")
        pane = tmux.panes("t")[0].pane_id
        await asyncio.sleep(0.5)
        marker = tmp_path / "ran.txt"
        tmux.send_keys(pane, f"echo ok > {marker}", "Enter")
        await wait_for(lambda: marker.exists())
        await server.start()
        tmux.send_keys(pane, "true", "Enter")
        await wait_for(lambda: got.of_type("shell.command_finished"))
        first_seq = got.messages[0][1]["seq"]
        assert first_seq > 1  # earlier events were lost while the tutor was down
    finally:
        tmux.kill_server()
        await server.stop()
