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

from cli_mentor.paths import AppPaths, session_key
from tests.conftest import require_tool

pytestmark = pytest.mark.e2e


@dataclass
class Rig:
    env: dict[str, str]
    home: Path
    ws: Path
    socket: str

    def mentor(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [sys.executable, "-m", "cli_mentor", *args], env=self.env, capture_output=True, text=True, timeout=60
        )
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
        CLI_MENTOR_MOCK_FIXTURE=str(Path(__file__).parents[1] / "fixtures" / "cpp_plan.json"),
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
    wait(lambda: "● observing" in r.screen(panes["tutor"]), what="tutor status line")
    session = r.tmux("list-sessions", "-F", "#{session_name}").split()[0]
    wait(
        lambda: "● observing" in r.tmux("show-options", "-v", "-t", session, "status-right"),
        what="tmux status bar filled in by the tutor",
    )
    return panes


def tutor_messages(r: Rig, trigger: str) -> list[str]:
    return [
        str(row[0])
        for row in r.db("SELECT content FROM messages WHERE role='tutor' AND trigger=? ORDER BY id", (trigger,))
    ]


def step_dir(r: Rig) -> Path:
    found = sorted(r.ws.glob("*/01-*"))
    assert found, "step folder not created"
    return found[0]


def to_first_step(r: Rig, tutor: str) -> Path:
    wait(lambda: "What would you like to learn?" in r.screen(tutor), what="welcome question")
    r.type(tutor, "I want to learn C++ from the very basics to the most advanced features")
    plan = wait(lambda: [m for m in tutor_messages(r, "plan") if "Module 1: First programs" in m], what="proposed plan")
    assert isinstance(plan, list) and "Reply **yes**" in plan[0]
    r.type(tutor, "yes")
    wait(lambda: "Create exercise files?" in r.screen(tutor), what="setup approval")
    r.key(tutor, "y")
    intro = wait(lambda: tutor_messages(r, "lesson_intro"), what="lesson intro")
    assert isinstance(intro, list) and "New words" in intro[0] and "Your task" not in intro[0]
    return step_dir(r)


def test_acceptance_learning_flow(rig: Rig) -> None:
    r = rig
    p = start(r)
    tutor, shell, editor = p["tutor"], p["shell"], p["editor"]
    assert "offline mock" in r.screen(tutor)

    # 1-3. Welcome asks for a goal; the goal becomes a plan; the first step explains before the task.
    wd = to_first_step(r, tutor)
    assert (wd / "hello.cpp").exists()
    r.type(tutor, "Hi")
    pred = wait(lambda: tutor_messages(r, "prediction"), what="prediction reply")
    assert isinstance(pred, list) and "Your task" in pred[0]

    # 4-6. A real compile error twice -> the actual error -> a small hint.
    (wd / "hello.cpp").write_text("int main() { std::cout << 1 }\n")
    r.type(shell, f"cd {wd}")
    r.type(shell, "c++ hello.cpp -o hello")
    time.sleep(1.0)
    r.type(shell, "c++ hello.cpp -o hello")
    hint = wait(lambda: tutor_messages(r, "repeated_failure"), timeout=25, what="unsolicited hint")
    assert isinstance(hint, list) and "error" in hint[0] and "Small hint (1)" in hint[0]

    # 7-9. Fix, compile, say "done" in plain English: the check runs the code (after approval)
    # -> evidence shows the help received.
    (wd / "hello.cpp").write_text('#include <iostream>\nint main() { std::cout << "Hello, C++!" << std::endl; }\n')
    r.type(shell, "c++ hello.cpp -o hello && ./hello")
    wait(
        lambda: r.db(
            "SELECT 1 FROM events WHERE type='shell.command_finished' AND payload LIKE '%./hello%' "
            "AND payload LIKE '%\"exit_status\": 0%'"
        ),
        what="successful compile",
    )
    r.type(tutor, "I think I'm done")
    wait(lambda: "Run your code to check it?" in r.screen(tutor), what="run-code approval")
    r.key(tutor, "y")
    rows = wait(
        lambda: r.db("SELECT result, assistance_level FROM skill_evidence WHERE kind='verification'"),
        timeout=30,
        what="verification evidence",
    )
    assert isinstance(rows, list) and all(row == ("pass", 1) for row in rows)
    reflection = wait(
        lambda: [m for m in tutor_messages(r, "verification") if "How would you print two lines?" in m],
        what="transfer question",
    )
    assert reflection
    r.type(tutor, "I would print a second message with a newline")
    wait(
        lambda: [m for m in tutor_messages(r, "lesson_intro") if "Variables and types" in m],
        what="next idea without a command",
    )

    # 12. Unsaved Neovim edits reach the tutor process; the disk is unchanged.
    notes = r.ws / "notes.txt"
    notes.write_text("saved line\n")
    r.key(editor, "Escape")
    r.type(editor, f":e {notes}")
    wait(
        lambda: r.db("SELECT 1 FROM events WHERE type='nvim.buffer_snapshot' AND payload LIKE '%saved line%'"),
        what="buffer snapshot",
    )
    r.key(editor, "o")
    r.type(editor, "UNSAVED-EDIT-42", enter=False)
    r.key(editor, "Escape")
    wait(
        lambda: r.db("SELECT 1 FROM events WHERE type='nvim.buffer_lines' AND payload LIKE '%UNSAVED-EDIT-42%'"),
        what="unsaved edit event",
    )
    assert notes.read_text() == "saved line\n"

    # 11. Pause: nothing new is recorded; resume asks first and resnapshots.
    r.type(tutor, "/observe pause")
    wait(lambda: "‖ paused" in r.screen(tutor), what="paused status")
    r.type(shell, "echo PAUSED-SECRET-77")
    time.sleep(1.0)
    assert not r.db("SELECT 1 FROM events WHERE payload LIKE '%PAUSED-SECRET-77%'")
    r.type(tutor, "/observe resume")
    wait(lambda: "Resume observation?" in r.screen(tutor), what="resume consent")
    r.key(tutor, "y")
    wait(lambda: "● observing" in r.screen(tutor), what="observation back on")
    wait(
        lambda: r.db("SELECT 1 FROM events WHERE type='tmux.snapshot' AND payload LIKE '%\"resume\"%'"),
        what="resume snapshot",
    )

    # 15. Ending the session warns about unsaved work, then leaves nothing running.
    r.key(tutor, "C-q")
    wait(lambda: "Leave CLI Mentor?" in r.screen(tutor), what="quit menu")
    r.key(tutor, "e")
    wait(lambda: "Unsaved work!" in r.screen(tutor), what="unsaved warning")
    r.key(tutor, "n")
    assert len(r.panes()) == 3
    r.key(editor, "Escape")
    r.type(editor, ":w")
    wait(lambda: "UNSAVED-EDIT-42" in notes.read_text(), what="saved file")
    time.sleep(0.5)
    r.key(tutor, "C-q")
    wait(lambda: "Leave CLI Mentor?" in r.screen(tutor), what="quit menu")
    r.key(tutor, "e")
    wait(
        lambda: subprocess.run(["tmux", "-L", r.socket, "list-sessions"], capture_output=True).returncode != 0,
        what="tmux server to exit",
    )
    time.sleep(0.5)
    leftovers = subprocess.run(["pgrep", "-f", str(r.home)], capture_output=True, text=True).stdout.strip()
    assert leftovers == "", f"orphaned processes: {leftovers}"
    sid = r.session_id()
    assert r.db("SELECT status FROM sessions WHERE id=?", (sid,))[0][0] == "ended"
    assert not (AppPaths.from_env(r.env).runtime_dir / session_key(sid)).exists()


def test_restart_resume_and_provider_failure(rig: Rig) -> None:
    r = rig
    # 14. A provider that cannot be used: clear message, mock fallback, workspace intact.
    p = start(r, "--provider", "anthropic")
    tutor = p["tutor"]
    wait(lambda: "No API key for anthropic" in r.screen(tutor), what="provider error note")
    assert "offline mock" in r.screen(tutor)
    to_first_step(r, tutor)
    r.type(tutor, "/hint")
    wait(lambda: tutor_messages(r, "hint"), what="hint")
    sid = r.session_id()

    # 10. The tutor process dies; the pane stays; `mentor resume` restarts it and offers to continue.
    pid = int(r.tmux("display-message", "-p", "-t", tutor, "#{pane_pid}").strip())
    os.kill(pid, 9)
    wait(lambda: r.tmux("display-message", "-p", "-t", tutor, "#{pane_dead}").strip() == "1", what="dead tutor pane")
    assert len(r.panes()) == 3
    assert "restarted" in r.mentor("resume", sid, "--detach").stdout
    wait(lambda: "Welcome back" in r.screen(tutor) and "Hello, C++" in r.screen(tutor), timeout=20, what="welcome back")
    assert r.db("SELECT hints_used FROM attempts")[0][0] == 1

    # Zooming a pane does not break observation.
    shell = p["shell"]
    r.tmux("resize-pane", "-Z", "-t", shell)
    r.type(shell, "echo zoomed-and-observed")
    wait(
        lambda: r.db(
            "SELECT 1 FROM events WHERE type='shell.command_finished' AND payload LIKE '%zoomed-and-observed%'"
        ),
        what="command seen while zoomed",
    )
    r.tmux("resize-pane", "-Z", "-t", shell)

    # `mentor stop` refuses while Neovim is open; --force closes only our session.
    proc = r.mentor("stop", sid, check=False)
    assert proc.returncode == 1 and "Neovim is still open" in proc.stdout
    r.mentor("stop", sid, "--force")
    wait(
        lambda: subprocess.run(["tmux", "-L", r.socket, "has-session"], capture_output=True).returncode != 0,
        what="session stopped",
    )
    assert sid in r.mentor("sessions").stdout
