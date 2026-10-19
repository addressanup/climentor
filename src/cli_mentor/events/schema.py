"""Versioned event envelope and payload types.

Clients (zsh hooks, the Neovim plugin) send *payloads* plus a per-source
sequence number. The engine wraps each payload in an ``EventEnvelope`` using
the identity from the authenticated connection, so a client cannot claim to be
a different source or session.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

SCHEMA_VERSION = 1
MAX_TEXT = 200_000
MAX_LINES = 20_000


class SourceKind(StrEnum):
    TMUX = "tmux"
    SHELL = "shell"
    NVIM = "nvim"
    WORKSPACE = "workspace"
    VERIFIER = "verifier"
    LEARNER = "learner"
    TUTOR = "tutor"


class Provenance(StrEnum):
    OBSERVED = "observed"  # a structured record from a hook or plugin
    SNAPSHOT = "snapshot"  # a rendered or full-state capture
    INFERRED = "inferred"  # derived by us (e.g. gap markers)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceRef(_Strict):
    kind: SourceKind
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:%@-]+$")
    epoch: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")


# ---------------------------------------------------------------- shell
class ShellSessionStarted(_Strict):
    type: Literal["shell.session_started"] = "shell.session_started"
    shell: str = Field(max_length=64)
    pid: int
    pane: str | None = Field(default=None, max_length=16)
    cwd: str = Field(max_length=4096)


class ShellCommandStarted(_Strict):
    type: Literal["shell.command_started"] = "shell.command_started"
    command: str = Field(max_length=8000)
    cwd: str = Field(max_length=4096)
    pane: str | None = Field(default=None, max_length=16)
    started_at: float


class ShellCommandFinished(_Strict):
    type: Literal["shell.command_finished"] = "shell.command_finished"
    command: str = Field(max_length=8000)
    cwd: str = Field(max_length=4096)  # working directory after the command
    exit_status: int
    pane: str | None = Field(default=None, max_length=16)
    started_at: float | None = None
    finished_at: float


# ---------------------------------------------------------------- tmux
class TmuxOutput(_Strict):
    type: Literal["tmux.output"] = "tmux.output"
    pane_id: str = Field(pattern=r"^%\d+$")
    text: str = Field(max_length=MAX_TEXT)
    buffered_ms: int | None = None  # from %extended-output, when available


class TmuxSnapshot(_Strict):
    type: Literal["tmux.snapshot"] = "tmux.snapshot"
    pane_id: str = Field(pattern=r"^%\d+$")
    lines: list[str] = Field(max_length=MAX_LINES)
    reason: Literal["baseline", "gap", "resume", "checkpoint", "reconnect"]
    current_command: str | None = None
    current_path: str | None = None


class TmuxPaneClosed(_Strict):
    type: Literal["tmux.pane_closed"] = "tmux.pane_closed"
    pane_id: str = Field(pattern=r"^%\d+$")


class TmuxFlow(_Strict):
    type: Literal["tmux.flow"] = "tmux.flow"
    pane_id: str = Field(pattern=r"^%\d+$")
    state: Literal["paused", "continued"]


class TmuxLayout(_Strict):
    type: Literal["tmux.layout"] = "tmux.layout"
    window_id: str
    layout: str = Field(max_length=4096)


# ---------------------------------------------------------------- neovim
class LineChange(_Strict):
    first: int = Field(ge=0)
    last: int = Field(ge=0)  # exclusive end in the old buffer
    new_last: int = Field(ge=0)  # exclusive end in the new buffer
    lines: list[str] = Field(max_length=MAX_LINES)


class NvimBufferSnapshot(_Strict):
    type: Literal["nvim.buffer_snapshot"] = "nvim.buffer_snapshot"
    buf: int
    path: str = Field(max_length=4096)
    filetype: str = Field(default="", max_length=64)
    changedtick: int
    modified: bool
    lines: list[str] = Field(max_length=MAX_LINES)
    reason: Literal["attach", "reload", "resync", "resume"] = "attach"


class NvimBufferLines(_Strict):
    type: Literal["nvim.buffer_lines"] = "nvim.buffer_lines"
    buf: int
    changedtick: int
    modified: bool
    changes: list[LineChange] = Field(max_length=5000)


class NvimBufferWritten(_Strict):
    type: Literal["nvim.buffer_written"] = "nvim.buffer_written"
    buf: int
    path: str = Field(max_length=4096)
    changedtick: int


class NvimBufferDetached(_Strict):
    type: Literal["nvim.buffer_detached"] = "nvim.buffer_detached"
    buf: int
    reason: str = Field(default="detach", max_length=64)


class NvimCursor(_Strict):
    type: Literal["nvim.cursor"] = "nvim.cursor"
    buf: int
    line: int = Field(ge=0)  # zero-based
    col: int = Field(ge=0)


class NvimMode(_Strict):
    type: Literal["nvim.mode"] = "nvim.mode"
    mode: str = Field(max_length=16)
    previous: str = Field(default="", max_length=16)


class Diagnostic(_Strict):
    line: int
    col: int
    severity: int = Field(ge=1, le=4)
    message: str = Field(max_length=2000)
    source: str = Field(default="", max_length=64)


class NvimDiagnostics(_Strict):
    type: Literal["nvim.diagnostics"] = "nvim.diagnostics"
    buf: int
    items: list[Diagnostic] = Field(max_length=500)


class NvimFocus(_Strict):
    type: Literal["nvim.focus"] = "nvim.focus"
    buf: int
    path: str = Field(max_length=4096)


class NvimTechnique(_Strict):
    """Opt-in counts of recognised Normal-mode commands in one exercise buffer."""

    type: Literal["nvim.technique"] = "nvim.technique"
    exercise_id: str = Field(max_length=128)
    buf: int
    counts: dict[str, int]


# ---------------------------------------------------------------- engine-made
class SourceStatusChanged(_Strict):
    type: Literal["source.status"] = "source.status"
    status: Literal["connected", "disconnected", "gap", "paused", "resumed"]
    detail: str = Field(default="", max_length=500)


Payload = Annotated[
    ShellSessionStarted
    | ShellCommandStarted
    | ShellCommandFinished
    | TmuxOutput
    | TmuxSnapshot
    | TmuxPaneClosed
    | TmuxFlow
    | TmuxLayout
    | NvimBufferSnapshot
    | NvimBufferLines
    | NvimBufferWritten
    | NvimBufferDetached
    | NvimCursor
    | NvimMode
    | NvimDiagnostics
    | NvimFocus
    | NvimTechnique
    | SourceStatusChanged,
    Field(discriminator="type"),
]

PAYLOAD_ADAPTER: TypeAdapter[Payload] = TypeAdapter(Payload)

# Which payload types each client kind may send over IPC.
ALLOWED_CLIENT_TYPES: dict[SourceKind, frozenset[str]] = {
    SourceKind.SHELL: frozenset(
        {"shell.session_started", "shell.command_started", "shell.command_finished"}
    ),
    SourceKind.NVIM: frozenset(
        {
            "nvim.buffer_snapshot",
            "nvim.buffer_lines",
            "nvim.buffer_written",
            "nvim.buffer_detached",
            "nvim.cursor",
            "nvim.mode",
            "nvim.diagnostics",
            "nvim.focus",
            "nvim.technique",
        }
    ),
}


class EventEnvelope(_Strict):
    schema_version: Literal[1] = 1
    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    session_id: str
    source: SourceRef
    seq: int = Field(ge=0)
    captured_at: float
    received_at: float
    ingest_index: int = 0
    provenance: Provenance = Provenance.OBSERVED
    payload: Payload

    @property
    def type(self) -> str:
        return self.payload.type
