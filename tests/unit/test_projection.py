from __future__ import annotations

import itertools

from cli_mentor.events.schema import (
    EventEnvelope,
    LineChange,
    NvimBufferLines,
    NvimBufferSnapshot,
    NvimBufferWritten,
    NvimMode,
    Payload,
    Provenance,
    ShellCommandFinished,
    ShellCommandStarted,
    SourceKind,
    SourceRef,
    SourceStatusChanged,
    TmuxFlow,
    TmuxOutput,
    TmuxSnapshot,
)
from cli_mentor.events.sequencing import Sequencer
from cli_mentor.state.projection import SyncStatus, WorkspaceState, strip_terminal

_counter = itertools.count(1)


def env(
    payload: Payload,
    kind: SourceKind = SourceKind.SHELL,
    sid: str = "zsh-1",
    seq: int | None = None,
    epoch: str = "e1",
) -> EventEnvelope:
    return EventEnvelope(
        session_id="s",
        source=SourceRef(kind=kind, id=sid, epoch=epoch),
        seq=seq if seq is not None else next(_counter),
        captured_at=1.0,
        received_at=2.0,
        provenance=Provenance.OBSERVED,
        payload=payload,
    )


def test_strip_terminal() -> None:
    raw = "\x1b[1;32mgreen\x1b[0m text\r\nprogress 10%\rprogress 100%\nab\x08c\x1b]0;title\x07"
    assert strip_terminal(raw) == "green text\nprogress 100%\nac"


def test_shell_commands_and_revisions() -> None:
    st = WorkspaceState()
    st.apply(env(ShellCommandStarted(command="ls", cwd="/w", started_at=1.0)))
    assert st.running and st.last_command() is None
    rev_before = st.key_revision("shell.last_exit")
    st.apply(env(ShellCommandFinished(command="ls", cwd="/w/sub", exit_status=2, started_at=1.0, finished_at=1.5)))
    last = st.last_command()
    assert last is not None and last.exit_status == 2 and last.duration == 0.5
    assert st.cwd == "/w/sub"
    assert st.key_revision("shell.last_exit") > rev_before
    assert not st.running


def test_gap_marks_incomplete_and_requests_resync() -> None:
    st = WorkspaceState()
    seqr = Sequencer()
    e1 = env(ShellCommandFinished(command="a", cwd="/", exit_status=0, finished_at=1), seq=1)
    st.apply(e1, seqr.check(e1.source, 1))
    e4 = env(ShellCommandFinished(command="b", cwd="/", exit_status=0, finished_at=2), seq=4)
    result = st.apply(e4, seqr.check(e4.source, 4))
    assert result.resync
    assert st.source_status(SourceKind.SHELL) == SyncStatus.INCOMPLETE
    assert st.shell_history_incomplete


def nvim_env(payload: Payload, seq: int) -> EventEnvelope:
    return env(payload, kind=SourceKind.NVIM, sid="nvim-1", seq=seq)


def test_buffer_snapshot_then_incremental_lines() -> None:
    st = WorkspaceState()
    st.apply(
        nvim_env(
            NvimBufferSnapshot(buf=1, path="/w/a.txt", changedtick=3, modified=False, lines=["one", "two", "three"]),
            1,
        )
    )
    # replace line 1, insert after, delete last
    st.apply(
        nvim_env(
            NvimBufferLines(
                buf=1,
                changedtick=4,
                modified=True,
                changes=[
                    LineChange(first=1, last=2, new_last=3, lines=["TWO", "two-and-half"]),
                    LineChange(first=3, last=4, new_last=3, lines=[]),
                ],
            ),
            2,
        )
    )
    mirror = st.buffer_for_path("/w/a.txt")
    assert mirror is not None
    assert mirror.lines == ["one", "TWO", "two-and-half"]
    assert mirror.modified and st.unsaved_buffers() == [mirror]
    # stale tick is ignored
    res = st.apply(
        nvim_env(
            NvimBufferLines(
                buf=1,
                changedtick=4,
                modified=True,
                changes=[LineChange(first=0, last=1, new_last=1, lines=["X"])],
            ),
            3,
        )
    )
    assert res.dropped == "stale changedtick"
    assert mirror.lines[0] == "one"
    st.apply(nvim_env(NvimBufferWritten(buf=1, path="/w/a.txt", changedtick=4), 4))
    assert not mirror.modified


def test_lines_without_baseline_request_resync() -> None:
    st = WorkspaceState()
    res = st.apply(nvim_env(NvimBufferLines(buf=9, changedtick=2, modified=True, changes=[]), 1))
    assert res.resync and res.dropped


def test_out_of_bounds_change_marks_incomplete_then_snapshot_recovers() -> None:
    st = WorkspaceState()
    st.apply(nvim_env(NvimBufferSnapshot(buf=1, path="/w/a", changedtick=1, modified=False, lines=["a"]), 1))
    res = st.apply(
        nvim_env(
            NvimBufferLines(
                buf=1,
                changedtick=2,
                modified=True,
                changes=[LineChange(first=5, last=7, new_last=6, lines=["x"])],
            ),
            2,
        )
    )
    assert res.resync
    assert st.source_status(SourceKind.NVIM) == SyncStatus.INCOMPLETE
    st.apply(
        nvim_env(
            NvimBufferSnapshot(buf=1, path="/w/a", changedtick=2, modified=True, lines=["a", "x"], reason="resync"),
            3,
        )
    )
    assert st.source_status(SourceKind.NVIM) == SyncStatus.CURRENT


def test_tmux_output_tail_and_snapshot_and_flow() -> None:
    st = WorkspaceState()
    tm = SourceKind.TMUX
    st.apply(env(TmuxOutput(pane_id="%1", text="\x1b[31merror:\x1b[0m nope\r\n"), kind=tm, sid="tmux-s"))
    assert st.panes["%1"].tail == "error: nope\n"
    st.apply(env(TmuxFlow(pane_id="%1", state="paused"), kind=tm, sid="tmux-s"))
    assert st.overall_sync() == SyncStatus.CATCHING_UP
    st.apply(env(TmuxSnapshot(pane_id="%1", lines=["$ ls", "a b"], reason="gap"), kind=tm, sid="tmux-s"))
    assert st.overall_sync() == SyncStatus.CURRENT
    assert st.panes["%1"].tail == ""  # stream before the gap is not continuous
    # editor panes: raw screen output is ignored
    st.apply(
        env(
            TmuxSnapshot(pane_id="%2", lines=[], reason="baseline", current_command="nvim"),
            kind=tm,
            sid="tmux-s",
        )
    )
    st.apply(env(TmuxOutput(pane_id="%2", text="\x1b[H~\r\n~"), kind=tm, sid="tmux-s"))
    assert st.panes["%2"].tail == ""


def test_source_status_and_pause() -> None:
    st = WorkspaceState()
    st.apply(env(SourceStatusChanged(status="disconnected"), kind=SourceKind.TMUX, sid="tmux-s"))
    assert st.overall_sync() == SyncStatus.DISCONNECTED
    st.paused = True
    assert st.overall_sync() == SyncStatus.PAUSED


def test_mode_history() -> None:
    st = WorkspaceState()
    for i, m in enumerate(["n", "i", "n"], start=1):
        st.apply(nvim_env(NvimMode(mode=m), i))
    assert [m for _t, m in st.editor.mode_history] == ["n", "i", "n"]
