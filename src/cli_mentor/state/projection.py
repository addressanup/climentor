"""Project events into the current workspace state.

``WorkspaceState`` is what the tutor "knows right now". Every change bumps the
global ``revision`` and the revision of each affected *key* (for example
``shell.last_exit`` or ``buffer:/path/file.py``). Tutor requests remember these
revisions so stale advice can be detected later.

In-memory state holds raw text (verification needs exact content). Anything
persisted or sent to a model is redacted first, elsewhere.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from ..events.schema import (
    Diagnostic,
    EventEnvelope,
    NvimBufferDetached,
    NvimBufferLines,
    NvimBufferSnapshot,
    NvimBufferWritten,
    NvimCursor,
    NvimDiagnostics,
    NvimFocus,
    NvimMode,
    NvimTechnique,
    ShellCommandFinished,
    ShellCommandStarted,
    ShellSessionStarted,
    SourceKind,
    SourceStatusChanged,
    TmuxFlow,
    TmuxLayout,
    TmuxOutput,
    TmuxPaneClosed,
    TmuxSnapshot,
)
from ..events.sequencing import SeqVerdict

TAIL_CHARS = 16_000
MAX_COMMANDS = 200
EDITOR_COMMANDS = frozenset({"nvim", "vim", "vi"})


class SyncStatus(StrEnum):
    CURRENT = "current"
    CATCHING_UP = "catching-up"
    DISCONNECTED = "disconnected"
    PAUSED = "paused"
    INCOMPLETE = "incomplete"


_SEVERITY = {
    SyncStatus.CURRENT: 0,
    SyncStatus.CATCHING_UP: 1,
    SyncStatus.INCOMPLETE: 2,
    SyncStatus.DISCONNECTED: 3,
    SyncStatus.PAUSED: 4,
}

# ---------------------------------------------------------------- terminal text
_ANSI = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL or ST
    r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1b[PX^_][^\x1b]*\x1b\\"  # DCS/SOS/PM/APC
    r"|\x1b[@-Z\\-_]"  # other two-byte escapes
    r"|\x1b[()][0-9A-Za-z]"  # charset selection
)
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def strip_terminal(text: str) -> str:
    """Approximate plain text from raw terminal output (not a terminal emulator)."""
    text = _ANSI.sub("", text).replace("\r\n", "\n")
    if "\x08" in text:
        out: list[str] = []
        for ch in text:
            if ch != "\x08":
                out.append(ch)
            elif out and out[-1] != "\n":
                out.pop()
        text = "".join(out)
    lines = [line.rsplit("\r", 1)[-1] for line in text.split("\n")]
    return _CTRL.sub("", "\n".join(lines))


# ---------------------------------------------------------------- state types
@dataclass
class CommandRecord:
    command: str
    cwd: str
    started_at: float | None
    pane: str | None
    source_id: str
    exit_status: int | None = None
    finished_at: float | None = None

    @property
    def running(self) -> bool:
        return self.exit_status is None

    @property
    def duration(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return max(0.0, self.finished_at - self.started_at)


@dataclass
class PaneView:
    pane_id: str
    tail: str = ""
    snapshot: list[str] = field(default_factory=list)
    snapshot_at: float | None = None
    snapshot_reason: str = ""
    current_command: str = ""
    last_output_at: float | None = None


@dataclass
class BufferMirror:
    source_id: str
    buf: int
    path: str
    filetype: str = ""
    changedtick: int = 0
    lines: list[str] = field(default_factory=list)
    modified: bool = False
    written_tick: int | None = None
    diagnostics: list[Diagnostic] = field(default_factory=list)
    updated_at: float = 0.0
    incomplete: bool = False

    @property
    def text(self) -> str:
        return "\n".join(self.lines) + ("\n" if self.lines else "")


@dataclass
class EditorView:
    mode: str = ""
    mode_history: deque[tuple[float, str]] = field(default_factory=lambda: deque(maxlen=200))
    cursor: tuple[str, int, int, int] | None = None  # source_id, buf, line, col
    focused: tuple[str, int] | None = None
    technique: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass
class SourceSync:
    kind: SourceKind
    id: str
    epoch: str
    status: SyncStatus = SyncStatus.CURRENT
    last_event_at: float = 0.0
    missing_total: int = 0
    detail: str = ""


@dataclass
class ApplyResult:
    changed: set[str] = field(default_factory=set)
    resync: bool = False  # caller should request a fresh snapshot from this source
    dropped: str = ""  # reason when the event was ignored


class WorkspaceState:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self.revision = 0
        self.key_revisions: dict[str, int] = {}
        self.commands: deque[CommandRecord] = deque(maxlen=MAX_COMMANDS)
        self.running: dict[str, CommandRecord] = {}
        self.cwd: str | None = None
        self.panes: dict[str, PaneView] = {}
        self.buffers: dict[tuple[str, int], BufferMirror] = {}
        self.editor = EditorView()
        self.sources: dict[tuple[str, str], SourceSync] = {}
        self.paused = False
        self.shell_history_incomplete = False

    # ------------------------------------------------------------ revisions
    def bump(self, keys: set[str]) -> None:
        if not keys:
            return
        self.revision += 1
        for key in keys:
            self.key_revisions[key] = self.revision

    def key_revision(self, key: str) -> int:
        return self.key_revisions.get(key, 0)

    def snapshot_revisions(self, keys: list[str] | None = None) -> dict[str, int]:
        if keys is None:
            return dict(self.key_revisions)
        return {k: self.key_revision(k) for k in keys}

    # ------------------------------------------------------------ queries
    def last_command(self) -> CommandRecord | None:
        return self.commands[-1] if self.commands else None

    def buffer_for_path(self, path: str) -> BufferMirror | None:
        best: BufferMirror | None = None
        for mirror in self.buffers.values():
            if mirror.path == path and (best is None or mirror.updated_at > best.updated_at):
                best = mirror
        return best

    def focused_buffer(self) -> BufferMirror | None:
        if self.editor.focused is None:
            return None
        return self.buffers.get(self.editor.focused)

    def unsaved_buffers(self) -> list[BufferMirror]:
        return [b for b in self.buffers.values() if b.modified]

    def overall_sync(self, kinds: set[SourceKind] | None = None) -> SyncStatus:
        if self.paused:
            return SyncStatus.PAUSED
        worst = SyncStatus.CURRENT
        for src in self.sources.values():
            if kinds is not None and src.kind not in kinds:
                continue
            if _SEVERITY[src.status] > _SEVERITY[worst]:
                worst = src.status
        return worst

    def source_status(self, kind: SourceKind) -> SyncStatus | None:
        statuses = [s.status for s in self.sources.values() if s.kind == kind]
        if not statuses:
            return None
        return max(statuses, key=lambda s: _SEVERITY[s])

    # ------------------------------------------------------------ apply
    def apply(self, env: EventEnvelope, verdict: SeqVerdict | None = None) -> ApplyResult:
        result = ApplyResult()
        src = self._source(env)
        src.last_event_at = env.received_at
        if verdict is not None and verdict.gap:
            src.missing_total += verdict.missing
            src.status = SyncStatus.INCOMPLETE
            src.detail = f"{verdict.missing} event(s) missing"
            result.resync = True
            if env.source.kind == SourceKind.SHELL:
                self.shell_history_incomplete = True
            result.changed.add(f"sync:{env.source.kind.value}")
        handler = getattr(self, "_on_" + env.payload.type.replace(".", "_"), None)
        if handler is not None:
            handler(env, result)
        self.bump(result.changed)
        return result

    def _source(self, env: EventEnvelope) -> SourceSync:
        key = (env.source.kind.value, env.source.id)
        src = self.sources.get(key)
        if src is None or src.epoch != env.source.epoch:
            src = SourceSync(env.source.kind, env.source.id, env.source.epoch)
            self.sources[key] = src
        return src

    # shell -----------------------------------------------------------------
    def _on_shell_session_started(self, env: EventEnvelope, r: ApplyResult) -> None:
        p = env.payload
        assert isinstance(p, ShellSessionStarted)
        self.cwd = p.cwd
        r.changed.add("shell.cwd")

    def _on_shell_command_started(self, env: EventEnvelope, r: ApplyResult) -> None:
        p = env.payload
        assert isinstance(p, ShellCommandStarted)
        self.running[env.source.id] = CommandRecord(p.command, p.cwd, p.started_at, p.pane, env.source.id)
        r.changed.add("shell.running")

    def _on_shell_command_finished(self, env: EventEnvelope, r: ApplyResult) -> None:
        p = env.payload
        assert isinstance(p, ShellCommandFinished)
        self.running.pop(env.source.id, None)
        self.commands.append(
            CommandRecord(
                p.command,
                p.cwd,
                p.started_at,
                p.pane,
                env.source.id,
                exit_status=p.exit_status,
                finished_at=p.finished_at,
            )
        )
        self.cwd = p.cwd
        r.changed |= {"shell.commands", "shell.last_exit", "shell.cwd", "shell.running"}

    # tmux ------------------------------------------------------------------
    def _pane(self, pane_id: str) -> PaneView:
        pane = self.panes.get(pane_id)
        if pane is None:
            pane = PaneView(pane_id)
            self.panes[pane_id] = pane
        return pane

    def _on_tmux_output(self, env: EventEnvelope, r: ApplyResult) -> None:
        p = env.payload
        assert isinstance(p, TmuxOutput)
        pane = self._pane(p.pane_id)
        if pane.current_command in EDITOR_COMMANDS:
            return
        pane.tail = (pane.tail + strip_terminal(p.text))[-TAIL_CHARS:]
        pane.last_output_at = env.received_at
        r.changed.add(f"terminal:{p.pane_id}")

    def _on_tmux_snapshot(self, env: EventEnvelope, r: ApplyResult) -> None:
        p = env.payload
        assert isinstance(p, TmuxSnapshot)
        pane = self._pane(p.pane_id)
        pane.snapshot = list(p.lines)
        pane.snapshot_at = env.received_at
        pane.snapshot_reason = p.reason
        pane.current_command = p.current_command or pane.current_command
        if p.reason in ("gap", "reconnect", "resume"):
            pane.tail = ""  # the stream before this point is not continuous
        src = self._source(env)
        if src.status in (SyncStatus.CATCHING_UP, SyncStatus.INCOMPLETE):
            src.status = SyncStatus.CURRENT
            src.detail = f"resynchronised by {p.reason} snapshot"
        r.changed.add(f"terminal:{p.pane_id}")

    def _on_tmux_pane_closed(self, env: EventEnvelope, r: ApplyResult) -> None:
        p = env.payload
        assert isinstance(p, TmuxPaneClosed)
        self.panes.pop(p.pane_id, None)
        r.changed.add(f"terminal:{p.pane_id}")

    def _on_tmux_flow(self, env: EventEnvelope, r: ApplyResult) -> None:
        p = env.payload
        assert isinstance(p, TmuxFlow)
        src = self._source(env)
        if p.state == "paused":
            src.status = SyncStatus.CATCHING_UP
            src.detail = f"tmux paused {p.pane_id}: output was dropped"
            r.resync = True
        r.changed.add("sync:tmux")

    def _on_tmux_layout(self, env: EventEnvelope, r: ApplyResult) -> None:
        assert isinstance(env.payload, TmuxLayout)

    # neovim ----------------------------------------------------------------
    def _on_nvim_buffer_snapshot(self, env: EventEnvelope, r: ApplyResult) -> None:
        p = env.payload
        assert isinstance(p, NvimBufferSnapshot)
        key = (env.source.id, p.buf)
        self.buffers[key] = BufferMirror(
            source_id=env.source.id,
            buf=p.buf,
            path=p.path,
            filetype=p.filetype,
            changedtick=p.changedtick,
            lines=list(p.lines),
            modified=p.modified,
            updated_at=env.received_at,
        )
        src = self._source(env)
        if src.status == SyncStatus.INCOMPLETE and not any(
            b.incomplete for b in self.buffers.values() if b.source_id == env.source.id
        ):
            src.status = SyncStatus.CURRENT
            src.detail = "resynchronised by snapshot"
        r.changed |= {f"buffer:{p.path}", "editor.buffers"}

    def _on_nvim_buffer_lines(self, env: EventEnvelope, r: ApplyResult) -> None:
        p = env.payload
        assert isinstance(p, NvimBufferLines)
        mirror = self.buffers.get((env.source.id, p.buf))
        if mirror is None or mirror.incomplete:
            r.resync = True
            r.dropped = "no baseline snapshot for buffer"
            return
        if p.changedtick <= mirror.changedtick:
            r.dropped = "stale changedtick"
            return
        lines = mirror.lines
        for change in p.changes:
            if change.first > change.last or change.last > len(lines):
                mirror.incomplete = True
                self._source(env).status = SyncStatus.INCOMPLETE
                r.resync = True
                r.dropped = "change outside buffer bounds"
                return
            lines[change.first : change.last] = change.lines
        mirror.changedtick = p.changedtick
        mirror.modified = p.modified
        mirror.updated_at = env.received_at
        r.changed.add(f"buffer:{mirror.path}")

    def _on_nvim_buffer_written(self, env: EventEnvelope, r: ApplyResult) -> None:
        p = env.payload
        assert isinstance(p, NvimBufferWritten)
        mirror = self.buffers.get((env.source.id, p.buf))
        if mirror is not None:
            mirror.written_tick = p.changedtick
            mirror.path = p.path
            if p.changedtick >= mirror.changedtick:
                mirror.modified = False
        r.changed |= {f"buffer:{p.path}", "editor.saved"}

    def _on_nvim_buffer_detached(self, env: EventEnvelope, r: ApplyResult) -> None:
        p = env.payload
        assert isinstance(p, NvimBufferDetached)
        mirror = self.buffers.pop((env.source.id, p.buf), None)
        if mirror is not None:
            r.changed |= {f"buffer:{mirror.path}", "editor.buffers"}

    def _on_nvim_cursor(self, env: EventEnvelope, r: ApplyResult) -> None:
        p = env.payload
        assert isinstance(p, NvimCursor)
        self.editor.cursor = (env.source.id, p.buf, p.line, p.col)
        r.changed.add("editor.cursor")

    def _on_nvim_mode(self, env: EventEnvelope, r: ApplyResult) -> None:
        p = env.payload
        assert isinstance(p, NvimMode)
        self.editor.mode = p.mode
        self.editor.mode_history.append((env.captured_at, p.mode))
        r.changed.add("editor.mode")

    def _on_nvim_diagnostics(self, env: EventEnvelope, r: ApplyResult) -> None:
        p = env.payload
        assert isinstance(p, NvimDiagnostics)
        mirror = self.buffers.get((env.source.id, p.buf))
        if mirror is not None:
            mirror.diagnostics = list(p.items)
            r.changed.add(f"diagnostics:{mirror.path}")

    def _on_nvim_focus(self, env: EventEnvelope, r: ApplyResult) -> None:
        p = env.payload
        assert isinstance(p, NvimFocus)
        self.editor.focused = (env.source.id, p.buf)
        r.changed.add("editor.focus")

    def _on_nvim_technique(self, env: EventEnvelope, r: ApplyResult) -> None:
        p = env.payload
        assert isinstance(p, NvimTechnique)
        merged = self.editor.technique.setdefault(p.exercise_id, {})
        for name, count in p.counts.items():
            merged[name] = merged.get(name, 0) + max(0, int(count))
        r.changed.add(f"technique:{p.exercise_id}")

    # engine-made -------------------------------------------------------------
    def _on_source_status(self, env: EventEnvelope, r: ApplyResult) -> None:
        p = env.payload
        assert isinstance(p, SourceStatusChanged)
        src = self._source(env)
        mapping = {
            "connected": SyncStatus.CURRENT,
            "resumed": SyncStatus.CURRENT,
            "disconnected": SyncStatus.DISCONNECTED,
            "gap": SyncStatus.INCOMPLETE,
            "paused": SyncStatus.PAUSED,
        }
        src.status = mapping[p.status]
        src.detail = p.detail
        r.changed.add(f"sync:{env.source.kind.value}")
