"""End-to-end acceptance with the real `mentor` command.

Real tmux (private socket), real zsh with session hooks, real Neovim with the
plugin, the real Textual tutor pane, and the deterministic mock provider.
Everything lives in temp folders; a fake HOME keeps your dotfiles out of it.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.conftest import require_tool

pytestmark = pytest.mark.e2e


@dataclass
class Rig:
    env: dict[str, str]
    home: Path
    ws: Path
    socket: str

    def mentor(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run([sys.executable, "-m", "cli_mentor", *args], env=self.env, capture_output=True,
                              text=True, timeout=60)
        if check and proc.returncode != 0:
            raise AssertionError(f"mentor {' '.join(args)} failed:\n{proc.stdout}\n{proc.stderr}")
        return proc

    def tmux(self, *args: str, check: bool = True) -> str:
        env = {k: v for k, v in self.env.items() if k not in ("TMUX", "TMUX_PANE")}
        proc = subprocess.run(["tmux", "-L", self.socket, *args], env=env, capture_output=True, text=True, timeout=10)
        if check and proc.returncode != 0:
            raise AssertionError(f"tmux {args} failed: {proc.stderr}")
        return proc.stdout

    def panes(self) -> dict[str, str]:
        out = self.tmux("list-panes", "-a", "-F", "#{@mentor_role} #{pane_id}")
        return dict(line.split(" ", 1) for line in out.splitlines() if " " in line)

    def type(self, pane: str, text: str, enter: bool = True) -> None:
        self.tmux("send-keys", "-t", pane, "-l", text)
        if enter:
            self.tmux("send-keys", "-t", pane, "Enter")

    def key(self, pane: str, *keys: str) -> None:
        self.tmux("send-keys", "-t", pane, *keys)

    def screen(self, pane: str) -> str:
        return self.tmux("capture-pane", "-p", "-t", pane, check=False)

    def db(self, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
        conn = sqlite3.connect(self.home / "data" / "mentor.sqlite3", timeout=5)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def session_id(self) -> str:
        return str(self.db("SELECT id FROM sessions ORDER BY created_at DESC LIMIT 1")[0][0])


def wait(pred: Callable[[], object], timeout: float = 15.0, what: str = "condition") -> object:
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        try:
            last = pred()
        except (sqlite3.OperationalError, AssertionError, KeyError, IndexError, OSError):
            last = None
        if last:
            return last
        time.sleep(0.15)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def rig(tmp_path: Path, short_tmp: Path) -> Iterator[Rig]:
    for tool in ("tmux", "nvim", "zsh"):
        require_tool(tool)
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    (fake_home / ".zshrc").write_text("PROMPT='learner %1~ %% '\n")
    socket = f"cme2e-{uuid.uuid4().hex[:8]}"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLI_MENTOR", "TMUX", "XDG_"))}
    env.update(
        HOME=str(fake_home),
        CLI_MENTOR_HOME=str(tmp_path / "mentor"),
        CLI_MENTOR_RUNTIME_DIR=str(short_tmp / "run"),
        CLI_MENTOR_TMUX_SOCKET=socket,
        PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring",
    )
    r = Rig(env, tmp_path / "mentor", (tmp_path / "ws").resolve(), socket)
    r.mentor("init", "--yes", "--provider", "mock", "--workspace", str(r.ws))
    yield r
    subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)


def start(r: Rig, *extra: str) -> dict[str, str]:
    out = r.mentor("start", "--detach", *extra).stdout
    assert "ready in tmux session" in out
    panes = wait(lambda: (p := r.panes()) and len(p) == 3 and p, what="three panes")
    assert isinstance(panes, dict)
    wait(lambda: "OBS: ON" in r.screen(panes["tutor"]), what="tutor status line")
    return panes


def tutor_messages(r: Rig, trigger: str) -> list[str]:
    return [str(row[0]) for row in r.db("SELECT content FROM messages WHERE role='tutor' AND trigger=? ORDER BY id", (trigger,))]


def test_acceptance_learning_flow(rig: Rig) -> None:
    r = rig
    p = start(r)
    tutor, shell, editor = p["tutor"], p["shell"], p["editor"]
    status = r.screen(tutor)
    assert "MODEL: MOCK" in status and "SYNC:" in status

    # 3. Explain before assigning.
    r.type(tutor, "/lesson start cli.pipelines")
    wait(lambda: "Create exercise files?" in r.screen(tutor), what="setup approval")
    r.key(tutor, "y")
    intro = wait(lambda: tutor_messages(r, "lesson_intro"), what="lesson intro")
    assert isinstance(intro, list)
    assert "New words" in intro[0] and "`|` — pipe" in intro[0] and "Your task" not in intro[0]
    assert (r.ws / "cli/03-pipelines/server.log").exists()
    r.type(tutor, "3")
    pred = wait(lambda: tutor_messages(r, "prediction"), what="prediction reply")
    assert isinstance(pred, list) and "Your task" in pred[0]

    # 4-6. Deliberate mistake twice -> the actual error -> a small hint.
    wd = r.ws / "cli/03-pipelines"
    r.type(shell, f"cd {wd}")
    r.type(shell, "grep ERROR sever.log")
    time.sleep(0.5)
    r.type(shell, "grep ERROR sever.log")
    hint = wait(lambda: tutor_messages(r, "repeated_failure"), timeout=20, what="unsolicited hint")
    assert isinstance(hint, list)
    assert "sever.log" in hint[0] and "No such file" in hint[0] and "Small hint (1)" in hint[0]

    # 7-9. Correction -> trusted check -> evidence shows the help received.
    r.type(shell, "grep ERROR server.log | wc -l > error_count.txt")
    rows = wait(lambda: r.db("SELECT result, assistance_level FROM skill_evidence WHERE exercise_id='cli.pipelines' AND kind='verification'"),
                what="verification evidence")
    assert isinstance(rows, list) and all(row == ("pass", 1) for row in rows)
    wait(lambda: "(verified)" in r.screen(tutor), what="verified status")

    # 12. Unsaved Neovim edits reach the tutor process; the disk is unchanged.
    notes = r.ws / "notes.txt"
    notes.write_text("saved line\n")
    r.key(editor, "Escape")
    r.type(editor, f":e {notes}")
    wait(lambda: r.db("SELECT 1 FROM events WHERE type='nvim.buffer_snapshot' AND payload LIKE '%saved line%'"), what="buffer snapshot")
    r.key(editor, "o")
    r.type(editor, "UNSAVED-EDIT-42", enter=False)
    r.key(editor, "Escape")
    wait(lambda: r.db("SELECT 1 FROM events WHERE type='nvim.buffer_lines' AND payload LIKE '%UNSAVED-EDIT-42%'"), what="unsaved edit event")
    assert notes.read_text() == "saved line\n"

    # 11. Pause: nothing new is recorded; resume asks first and resnapshots.
    r.type(tutor, "/observe pause")
    wait(lambda: "OBS: PAUSED" in r.screen(tutor), what="paused status")
    r.type(shell, "echo PAUSED-SECRET-77")
    time.sleep(1.0)
    assert not r.db("SELECT 1 FROM events WHERE payload LIKE '%PAUSED-SECRET-77%'")
    r.type(tutor, "/observe resume")
    wait(lambda: "Resume observation?" in r.screen(tutor), what="resume consent")
    r.key(tutor, "y")
    wait(lambda: "OBS: ON" in r.screen(tutor), what="observation back on")
    wait(lambda: r.db("SELECT 1 FROM events WHERE type='tmux.snapshot' AND payload LIKE '%\"resume\"%'"), what="resume snapshot")
    assert not r.db("SELECT 1 FROM events WHERE payload LIKE '%PAUSED-SECRET-77\"%' AND type LIKE 'shell.%'")

    # 15. Ending the session warns about unsaved work, then leaves nothing running.
    r.key(tutor, "C-q")
    wait(lambda: "Leave CLI Mentor?" in r.screen(tutor), what="quit menu")
    r.key(tutor, "e")
    wait(lambda: "Unsaved work!" in r.screen(tutor), what="unsaved warning")
    r.key(tutor, "n")
    assert len(r.panes()) == 3  # nothing was closed
    r.key(editor, "Escape")
    r.type(editor, ":w")
    wait(lambda: "UNSAVED-EDIT-42" in notes.read_text(), what="saved file")
    time.sleep(0.5)
    r.key(tutor, "C-q")
    wait(lambda: "Leave CLI Mentor?" in r.screen(tutor), what="quit menu")
    r.key(tutor, "e")
    wait(lambda: subprocess.run(["tmux", "-L", r.socket, "list-sessions"], capture_output=True).returncode != 0,
         what="tmux server to exit")
    time.sleep(0.5)
    leftovers = subprocess.run(["pgrep", "-f", str(r.home)], capture_output=True, text=True).stdout.strip()
    assert leftovers == "", f"orphaned processes: {leftovers}"
    sid = r.session_id()
    assert r.db("SELECT status FROM sessions WHERE id=?", (sid,))[0][0] == "ended"
    assert not (Path(r.env["CLI_MENTOR_RUNTIME_DIR"]) / sid[:12]).exists()


def test_restart_resume_and_provider_failure(rig: Rig) -> None:
    r = rig
    # 14. A provider that cannot be used: clear message, mock fallback, workspace intact.
    p = start(r, "--provider", "anthropic")
    tutor = p["tutor"]
    wait(lambda: "No API key for anthropic" in r.screen(tutor), what="provider error note")
    assert "MODEL: MOCK" in r.screen(tutor)
    r.type(tutor, "/lesson start cli.files")
    wait(lambda: "Create exercise files?" in r.screen(tutor), what="setup approval")
    r.key(tutor, "y")
    wait(lambda: tutor_messages(r, "lesson_intro"), what="lesson intro")
    r.type(tutor, "/hint")
    wait(lambda: tutor_messages(r, "hint"), what="hint")
    sid = r.session_id()

    # 10. The tutor process dies; the pane stays; `mentor resume` restarts it with memory.
    pid = int(r.tmux("display-message", "-p", "-t", tutor, "#{pane_pid}").strip())
    os.kill(pid, 9)
    wait(lambda: r.tmux("display-message", "-p", "-t", tutor, "#{pane_dead}").strip() == "1", what="dead tutor pane")
    assert len(r.panes()) == 3  # editor and shell are untouched
    out = r.mentor("resume", sid, "--detach").stdout
    assert "restarted" in out
    wait(lambda: "Welcome back" in r.screen(tutor) and "Tidy the desk" in r.screen(tutor), timeout=20, what="welcome back")
    assert r.db("SELECT hints_used FROM attempts WHERE exercise_id='cli.files'")[0][0] == 1

    # `mentor stop` refuses while Neovim is open; --force closes only our session.
    proc = r.mentor("stop", sid, check=False)
    assert proc.returncode == 1 and "Neovim is still open" in proc.stdout
    r.mentor("stop", sid, "--force")
    wait(lambda: subprocess.run(["tmux", "-L", r.socket, "has-session"], capture_output=True).returncode != 0,
         what="session stopped")
    sessions = r.mentor("sessions").stdout
    assert sid in sessions and "ended" in sessions
