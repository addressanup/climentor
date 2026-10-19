"""Terminal observation through tmux control mode.

Responsibilities:
- connect (and reconnect with backoff) to our own tmux session;
- discard the tutor pane's output (no feedback loops);
- take ``capture-pane`` snapshots as a baseline, after gaps, after reconnects;
- notice panes that appear or close;
- handle tmux flow control (``%pause`` -> continue + snapshot).

A snapshot is the *rendered screen*. It is not proof of command history.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..events.schema import (
    Payload,
    Provenance,
    SourceStatusChanged,
    TmuxFlow,
    TmuxLayout,
    TmuxOutput,
    TmuxPaneClosed,
    TmuxSnapshot,
)
from ..tmuxutil import PANE_FORMAT, Tmux
from .tmux_control import Notification, TmuxControlClient

log = logging.getLogger(__name__)

Emit = Callable[[str, int, Payload, Provenance], None]  # epoch, seq, payload, provenance
SnapshotReason = str

EDITOR_COMMANDS = frozenset({"nvim", "vim", "vi"})


@dataclass
class PaneState:
    pane_id: str
    role: str
    current_command: str = ""
    current_path: str = ""


class TerminalObserver:
    def __init__(
        self,
        tmux: Tmux,
        session: str,
        emit: Emit,
        *,
        tutor_pane: str | None,
        pause_after: int = 5,
        history_lines: int = 200,
    ) -> None:
        self.tmux = tmux
        self.session = session
        self.emit_cb = emit
        self.tutor_pane = tutor_pane
        self.pause_after = pause_after
        self.history_lines = history_lines
        self.panes: dict[str, PaneState] = {}
        self.client: TmuxControlClient | None = None
        self.status = "disconnected"
        self._epoch = ""
        self._seq = 0
        self._connections = 0
        self._stopping = False
        self._supervisor: asyncio.Task[None] | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._recovering: set[str] = set()

    # ------------------------------------------------------------ public
    async def start(self, snapshot_reason: SnapshotReason = "baseline") -> None:
        self._stopping = False
        await self._connect(snapshot_reason)
        self._supervisor = asyncio.create_task(self._supervise(), name="tmux-observer")

    async def stop(self) -> None:
        """Close the control connection. tmux then sends us nothing at all."""
        self._stopping = True
        for task in (self._supervisor, self._refresh_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._supervisor = None
        if self.client is not None:
            await self.client.stop()
            self.client = None
        self.status = "disconnected"

    async def snapshot(self, reason: SnapshotReason = "checkpoint") -> None:
        for pane in list(self.panes.values()):
            await self._snapshot_pane(pane, reason)

    def observed_pane_ids(self) -> list[str]:
        return list(self.panes)

    # ------------------------------------------------------------ internals
    def _emit(self, payload: Payload, provenance: Provenance = Provenance.OBSERVED) -> None:
        self._seq += 1
        self.emit_cb(self._epoch, self._seq, payload, provenance)

    async def _connect(self, reason: SnapshotReason) -> None:
        self._connections += 1
        self._epoch = f"c{self._connections}-{int(time.time())}"
        self._seq = 0
        ignored = {self.tutor_pane} if self.tutor_pane else set()
        client = TmuxControlClient(
            self.tmux,
            self.session,
            on_output=self._on_output,
            on_notify=self._on_notify,
            pause_after=self.pause_after,
            ignored_panes=ignored,
        )
        await client.start()
        self.client = client
        self.status = "current"
        self._emit(SourceStatusChanged(status="connected", detail=reason), Provenance.INFERRED)
        await self._refresh_panes(initial_reason=reason)

    async def _supervise(self) -> None:
        backoff = 0.5
        while not self._stopping:
            client = self.client
            if client is None:
                return
            await client.closed.wait()
            if self._is_stopping():
                return
            self.status = "disconnected"
            self._emit(
                SourceStatusChanged(status="disconnected", detail=client.exit_reason or "closed"),
                Provenance.INFERRED,
            )
            while not self._stopping:
                await asyncio.sleep(backoff)
                if not await asyncio.to_thread(self.tmux.has_session, self.session):
                    log.info("tmux session %s is gone; observer stopping", self.session)
                    return
                try:
                    await self._connect("reconnect")
                    backoff = 0.5
                    break
                except Exception as exc:
                    log.warning("tmux reconnect failed: %s", exc)
                    backoff = min(backoff * 2, 10.0)

    def _is_stopping(self) -> bool:
        return self._stopping

    async def _list_panes(self) -> list[PaneState]:
        assert self.client is not None
        response = await self.client.command(
            "list-panes", "-s", "-t", f"={self.session}", "-F", PANE_FORMAT
        )
        result: list[PaneState] = []
        for line in response.lines:
            parts = line.split("\t")
            if len(parts) != 10 or parts[0] == self.tutor_pane or parts[3] == "1":
                continue
            result.append(PaneState(parts[0], parts[8] or "other", parts[4], parts[5]))
        return result

    async def _refresh_panes(self, initial_reason: SnapshotReason | None = None) -> None:
        if self.client is None or not self.client.running:
            return
        try:
            current = {p.pane_id: p for p in await self._list_panes()}
        except Exception as exc:
            log.warning("list-panes failed: %s", exc)
            return
        for pane_id in list(self.panes):
            if pane_id not in current:
                del self.panes[pane_id]
                self._emit(TmuxPaneClosed(pane_id=pane_id))
        for pane_id, state in current.items():
            is_new = pane_id not in self.panes
            self.panes[pane_id] = state
            if initial_reason is not None or is_new:
                await self._snapshot_pane(state, initial_reason or "baseline")

    def _schedule_refresh(self) -> None:
        if self._refresh_task is not None and not self._refresh_task.done():
            return

        async def later() -> None:
            await asyncio.sleep(0.1)
            await self._refresh_panes()

        self._refresh_task = asyncio.create_task(later())

    async def _snapshot_pane(self, pane: PaneState, reason: SnapshotReason) -> None:
        if self.client is None or not self.client.running:
            return
        if pane.current_command in EDITOR_COMMANDS:
            return  # editor state comes from the Neovim plugin, not the screen
        try:
            lines = await self.client.capture(pane.pane_id, self.history_lines)
        except Exception as exc:
            log.warning("capture-pane %s failed: %s", pane.pane_id, exc)
            return
        self._emit(
            TmuxSnapshot(
                pane_id=pane.pane_id,
                lines=lines,
                reason=reason,  # type: ignore[arg-type]
                current_command=pane.current_command or None,
                current_path=pane.current_path or None,
            ),
            Provenance.SNAPSHOT,
        )

    def _on_output(self, pane_id: str, text: str, buffered_ms: int | None) -> None:
        if pane_id not in self.panes:
            self._schedule_refresh()
        self._emit(TmuxOutput(pane_id=pane_id, text=text, buffered_ms=buffered_ms))

    def _on_notify(self, note: Notification) -> None:
        name = note.name
        if name == "pause":
            pane = note.args.decode("ascii", "replace").strip()
            self.status = "catching-up"
            self._emit(TmuxFlow(pane_id=pane, state="paused"))
            asyncio.get_running_loop().create_task(self._recover_pane(pane))
        elif name == "continue":
            pane = note.args.decode("ascii", "replace").strip()
            self._emit(TmuxFlow(pane_id=pane, state="continued"))
        elif name == "layout-change":
            parts = note.args.decode("utf-8", "replace").split(" ")
            if len(parts) >= 2:
                self._emit(TmuxLayout(window_id=parts[0], layout=parts[1]))
            self._schedule_refresh()
        elif name in ("window-close", "window-add", "window-pane-changed", "sessions-changed"):
            self._schedule_refresh()

    async def _recover_pane(self, pane_id: str) -> None:
        """tmux paused a pane because we fell behind: output was dropped. Resync."""
        if pane_id in self._recovering or self.client is None:
            return
        self._recovering.add(pane_id)
        try:
            await self.client.continue_pane(pane_id)
            state = self.panes.get(pane_id)
            if state is not None:
                await self._snapshot_pane(state, "gap")
            self.status = "current"
        except Exception as exc:
            log.warning("recovering pane %s failed: %s", pane_id, exc)
        finally:
            self._recovering.discard(pane_id)
