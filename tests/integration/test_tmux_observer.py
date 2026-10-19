"""Real tmux server (private socket) observed through control mode."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from cli_mentor.events.schema import (
    Payload,
    Provenance,
    SourceStatusChanged,
    TmuxFlow,
    TmuxOutput,
    TmuxPaneClosed,
    TmuxSnapshot,
)
from cli_mentor.observe.terminal import TerminalObserver
from cli_mentor.tmuxutil import Tmux
from tests.conftest import require_tool
from tests.helpers import private_tmux, wait_for

pytestmark = pytest.mark.integration


class Sink:
    def __init__(self) -> None:
        self.items: list[tuple[str, int, Payload, Provenance]] = []

    def __call__(self, epoch: str, seq: int, payload: Payload, prov: Provenance) -> None:
        self.items.append((epoch, seq, payload, prov))

    def of(self, cls: type[Any]) -> list[Any]:
        return [p for _e, _s, p, _pr in self.items if isinstance(p, cls)]

    def output_for(self, pane: str) -> str:
        return "".join(p.text for p in self.of(TmuxOutput) if p.pane_id == pane)


@pytest.fixture
def tmux() -> Iterator[Tmux]:
    require_tool("tmux")
    t = private_tmux()
    # Left pane plays "tutor" (cat echoes typed text), right pane is a plain shell.
    t.run("new-session", "-d", "-s", "obs", "-x", "160", "-y", "40", "cat")
    t.run("split-window", "-h", "-t", "=obs:", "/bin/sh")
    panes = t.panes("obs")
    t.run("set-option", "-p", "-t", panes[0].pane_id, "@mentor_role", "tutor")
    t.run("set-option", "-p", "-t", panes[1].pane_id, "@mentor_role", "shell")
    yield t
    t.kill_server()


def pane_ids(t: Tmux) -> tuple[str, str]:
    panes = t.panes("obs")
    return panes[0].pane_id, panes[1].pane_id


async def test_output_attribution_and_tutor_exclusion(tmux: Tmux) -> None:
    tutor, shell = pane_ids(tmux)
    sink = Sink()
    obs = TerminalObserver(tmux, "obs", sink, tutor_pane=tutor)
    await obs.start()
    try:
        baseline = sink.of(TmuxSnapshot)
        assert [s.pane_id for s in baseline] == [shell]
        assert baseline[0].reason == "baseline"
        tmux.send_keys(shell, "echo attrib-$((40+2))", "Enter")
        tmux.send_keys(tutor, "tutor-secret-text", "Enter")
        await wait_for(lambda: "attrib-42" in sink.output_for(shell))
        await asyncio.sleep(0.3)
        assert all(p.pane_id != tutor for p in sink.of(TmuxOutput))
        assert "tutor-secret-text" not in "".join(p.text for p in sink.of(TmuxOutput))
        # Sequence numbers increase within the epoch.
        seqs = [s for _e, s, _p, _pr in sink.items]
        assert seqs == list(range(1, len(seqs) + 1))
        # On-demand snapshot is the rendered screen.
        await obs.snapshot("checkpoint")
        snap = sink.of(TmuxSnapshot)[-1]
        assert any("attrib-42" in line for line in snap.lines)
    finally:
        await obs.stop()


async def test_reconnect_after_control_client_dies(tmux: Tmux) -> None:
    tutor, shell = pane_ids(tmux)
    sink = Sink()
    obs = TerminalObserver(tmux, "obs", sink, tutor_pane=tutor)
    await obs.start()
    try:
        first_epoch = sink.items[0][0]
        assert obs.client is not None and obs.client._proc is not None
        obs.client._proc.kill()
        await wait_for(lambda: any(s.reason == "reconnect" for s in sink.of(TmuxSnapshot)), timeout=10)
        statuses = [s.status for s in sink.of(SourceStatusChanged)]
        assert "disconnected" in statuses and statuses.count("connected") == 2
        assert sink.items[-1][0] != first_epoch
        tmux.send_keys(shell, "echo after-reconnect", "Enter")
        await wait_for(lambda: "after-reconnect" in sink.output_for(shell))
    finally:
        await obs.stop()


async def test_pane_close_and_new_pane(tmux: Tmux) -> None:
    tutor, shell = pane_ids(tmux)
    sink = Sink()
    obs = TerminalObserver(tmux, "obs", sink, tutor_pane=tutor)
    await obs.start()
    try:
        tmux.run("split-window", "-v", "-t", shell, "/bin/sh")
        await wait_for(lambda: len(obs.panes) == 2)
        new_pane = next(p for p in obs.panes if p != shell)
        await wait_for(lambda: any(s.pane_id == new_pane for s in sink.of(TmuxSnapshot)))
        tmux.run("kill-pane", "-t", new_pane)
        await wait_for(lambda: any(c.pane_id == new_pane for c in sink.of(TmuxPaneClosed)))
        assert new_pane not in obs.panes
    finally:
        await obs.stop()


async def test_flow_control_pause_recovers_with_snapshot(tmux: Tmux) -> None:
    tutor, shell = pane_ids(tmux)
    sink = Sink()
    obs = TerminalObserver(tmux, "obs", sink, tutor_pane=tutor, pause_after=1)
    await obs.start()
    try:
        assert obs.client is not None
        # Ask tmux to pause the pane for this client, as it would when we lag.
        await obs.client.command("refresh-client", "-A", f"{shell}:pause")
        await wait_for(lambda: any(f.state == "paused" for f in sink.of(TmuxFlow)))
        await wait_for(lambda: any(s.reason == "gap" for s in sink.of(TmuxSnapshot)))
        tmux.send_keys(shell, "echo resumed-ok", "Enter")
        await wait_for(lambda: "resumed-ok" in sink.output_for(shell))
    finally:
        await obs.stop()


async def test_output_burst_is_captured(tmux: Tmux) -> None:
    tutor, shell = pane_ids(tmux)
    sink = Sink()
    obs = TerminalObserver(tmux, "obs", sink, tutor_pane=tutor)
    await obs.start()
    try:
        tmux.send_keys(shell, "seq 1 20000; echo burst-done", "Enter")
        await wait_for(
            lambda: "burst-done\r\n" in sink.output_for(shell) or any(s.reason == "gap" for s in sink.of(TmuxSnapshot)),
            timeout=20,
        )
        text = sink.output_for(shell)
        if not any(s.reason == "gap" for s in sink.of(TmuxSnapshot)):
            assert "19999\r\n20000\r\n" in text
    finally:
        await obs.stop()


async def test_stop_closes_connection(tmux: Tmux) -> None:
    tutor, shell = pane_ids(tmux)
    sink = Sink()
    obs = TerminalObserver(tmux, "obs", sink, tutor_pane=tutor)
    await obs.start()
    await obs.stop()
    count = len(sink.items)
    tmux.send_keys(shell, "echo while-stopped", "Enter")
    await asyncio.sleep(0.4)
    assert len(sink.items) == count
    clients = tmux.run("list-clients", "-F", "#{client_control_mode}", check=False)
    assert "1" not in clients.split()
