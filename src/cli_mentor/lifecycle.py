"""Create, attach to, repair, and stop the dedicated tmux workspace.

Everything happens on our own tmux server (``tmux -L cli-mentor``). We only
ever kill sessions we created there, and we never touch the learner's
default tmux server.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .config import Config
from .observe.ipc import new_token
from .observe.shell import prepare_zdotdir, shell_environment
from .paths import AppPaths, ensure_private_dir, write_private_file
from .tmuxutil import DEFAULT_SOCKET, Tmux, TmuxError

SESSION_PREFIX = "mentor-"
PASSTHROUGH_ENV = (
    "CLI_MENTOR_HOME",
    "CLI_MENTOR_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "PYTHON_KEYRING_BACKEND",
    "CLI_MENTOR_DEMO",
)
SECRET_ENV = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")

TMUX_CONF = """\
# CLI Mentor's private tmux settings (only for the dedicated 'cli-mentor' server).
# Your own ~/.tmux.conf is loaded first, if you have one.
if-shell "test -f ~/.tmux.conf" "source-file ~/.tmux.conf"
set -g mouse on
set -g focus-events on
set -sg escape-time 10
set -g history-limit 10000
set -g default-terminal "tmux-256color"
set -g status on
set -g status-interval 2
set -g status-left " CLI Mentor "
set -g status-left-length 14
set -g status-right-length 260
set -g pane-border-status top
set -g pane-border-format " #{?pane_active,>> ,}#{@mentor_role} #{pane_current_command} "
"""


@dataclass(frozen=True)
class WorkspaceInfo:
    session_id: str
    tmux_session: str
    tutor_pane: str
    editor_pane: str
    shell_pane: str


def tmux_session_name(session_id: str) -> str:
    return SESSION_PREFIX + session_id[-6:]


def plugin_dir() -> Path:
    return Path(str(resources.files("cli_mentor").joinpath("nvim")))


def socket_name() -> str:
    return os.environ.get("CLI_MENTOR_TMUX_SOCKET", DEFAULT_SOCKET)


def make_tmux(paths: AppPaths) -> Tmux:
    conf = paths.config_dir / "tmux.conf"
    if not conf.exists() or conf.read_text() != TMUX_CONF:
        paths.config_dir.mkdir(parents=True, exist_ok=True)
        conf.write_text(TMUX_CONF)
    return Tmux(socket_name(), config_file=str(conf))


def _env_args(env: dict[str, str]) -> list[str]:
    out: list[str] = []
    for key, value in env.items():
        out += ["-e", f"{key}={value}"]
    return out


def _without_secrets(command: str) -> str:
    unset = " ".join(f"-u {v}" for v in SECRET_ENV)
    return f"env {unset} {command}"


def ensure_token(paths: AppPaths, session_id: str) -> Path:
    run_dir = paths.session_runtime_dir(session_id)
    token_file = run_dir / "token"
    if not token_file.exists() or len(token_file.read_text().strip()) < 64:
        write_private_file(token_file, new_token())
    return token_file


def tutor_command(session_id: str) -> str:
    return f"{shlex.quote(sys.executable)} -m cli_mentor tutor --session {shlex.quote(session_id)}"


def create_workspace(
    config: Config,
    paths: AppPaths,
    session_id: str,
    *,
    width: int = 200,
    height: int = 50,
    subject_folder: str | None = None,
) -> WorkspaceInfo:
    tmux = make_tmux(paths)
    name = tmux_session_name(session_id)
    if tmux.has_session(name):
        return describe(tmux, session_id)
    root = Path(config.workspace_root).expanduser()
    if not root.is_dir():
        raise RuntimeError(f"Practice folder {root} does not exist. Run `mentor init`.")
    # Panes open in the subject's own folder (created inside the practice folder only).
    start_dir = (
        root / subject_folder if subject_folder and "/" not in subject_folder and subject_folder != ".." else root
    )
    start_dir.mkdir(parents=True, exist_ok=True)
    start = str(start_dir)
    run_dir = ensure_private_dir(paths.session_runtime_dir(session_id))
    token_file = ensure_token(paths, session_id)
    sock = paths.socket_path(session_id)
    zdot = prepare_zdotdir(run_dir)
    nvim_bin = shutil.which("nvim") or "nvim"
    zsh_bin = shutil.which("zsh") or "/bin/zsh"
    shell_env = shell_environment(
        zdotdir=zdot,
        socket_path=sock,
        token_file=token_file,
        nvim_rtp=plugin_dir(),
        user_zdotdir=os.environ.get("ZDOTDIR"),
    )
    editor_env = {"CLI_MENTOR_SOCK": str(sock), "CLI_MENTOR_TOKEN_FILE": str(token_file)}
    tutor_env = {k: os.environ[k] for k in PASSTHROUGH_ENV if k in os.environ}
    tutor_env["CLI_MENTOR_TMUX_SOCKET"] = tmux.socket_name
    tutor_env["CLI_MENTOR_TMUX_CONF"] = tmux.config_file or ""
    nvim_cmd = (
        f"{shlex.quote(nvim_bin)} --cmd {shlex.quote('set rtp^=' + str(plugin_dir()))} "
        f"--cmd {shlex.quote('lua require([[cli_mentor]]).start()')}"
    )
    created = False
    try:
        tmux.run(
            "new-session",
            "-d",
            "-s",
            name,
            "-x",
            str(width),
            "-y",
            str(height),
            "-c",
            start,
            *_env_args(tutor_env),
            tutor_command(session_id),
        )
        created = True
        tutor = tmux.panes(name)[0].pane_id
        tmux.run("set-option", "-p", "-t", tutor, "@mentor_role", "tutor")
        tmux.run("set-option", "-p", "-t", tutor, "remain-on-exit", "on")
        tmux.run(
            "split-window",
            "-h",
            "-l",
            "62%",
            "-t",
            tutor,
            "-c",
            start,
            *_env_args(editor_env),
            _without_secrets(nvim_cmd),
        )
        editor = next(p.pane_id for p in tmux.panes(name) if p.pane_id != tutor)
        tmux.run("set-option", "-p", "-t", editor, "@mentor_role", "editor")
        tmux.run(
            "split-window",
            "-v",
            "-l",
            "40%",
            "-t",
            editor,
            "-c",
            start,
            *_env_args(shell_env),
            _without_secrets(f"{shlex.quote(zsh_bin)} -l"),
        )
        shell = next(p.pane_id for p in tmux.panes(name) if p.pane_id not in (tutor, editor))
        tmux.run("set-option", "-p", "-t", shell, "@mentor_role", "shell")
        tmux.run("select-pane", "-t", tutor)
    except Exception:
        if created:
            tmux.kill_session(name)  # roll back only what we created
        raise
    return WorkspaceInfo(session_id, name, tutor, editor, shell)


def describe(tmux: Tmux, session_id: str) -> WorkspaceInfo:
    name = tmux_session_name(session_id)
    roles = {p.role: p.pane_id for p in tmux.panes(name)}
    return WorkspaceInfo(session_id, name, roles.get("tutor", ""), roles.get("editor", ""), roles.get("shell", ""))


def repair_tutor(paths: AppPaths, session_id: str) -> bool:
    """Respawn the tutor pane if its process died. Returns True if respawned."""
    tmux = make_tmux(paths)
    name = tmux_session_name(session_id)
    for pane in tmux.panes(name):
        if pane.role == "tutor" and pane.dead:
            tmux.run("respawn-pane", "-k", "-t", pane.pane_id, tutor_command(session_id))
            return True
    return False


def attach_argv(paths: AppPaths, session_id: str) -> list[str]:
    tmux = make_tmux(paths)
    return tmux.argv("attach-session", "-t", f"={tmux_session_name(session_id)}")


def attach(paths: AppPaths, session_id: str, nested: bool = False) -> int:  # replaces this process
    argv = attach_argv(paths, session_id)
    if os.environ.get("TMUX") and not nested:
        print("You are already inside tmux. To avoid nesting, open a NEW terminal window and run:\n")
        print("    " + " ".join(shlex.quote(a) for a in argv))
        print("\nOr run `mentor resume --nested` to attach inside this tmux (prefix keys may clash).")
        return 0
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    os.execvpe(argv[0], argv, env)


def editor_busy(paths: AppPaths, session_id: str) -> list[str]:
    tmux = make_tmux(paths)
    out = []
    for pane in tmux.panes(tmux_session_name(session_id)):
        if pane.role == "editor" and pane.current_command in ("nvim", "vim"):
            out.append("Neovim is still open (unsaved changes would be lost)")
        if pane.role == "shell" and pane.current_command not in ("zsh", "-zsh", "bash", "sh", ""):
            out.append(f"the shell is running `{pane.current_command}`")
    return out


def stop_workspace(paths: AppPaths, session_id: str) -> bool:
    tmux = make_tmux(paths)
    name = tmux_session_name(session_id)
    if not tmux.has_session(name):
        return False
    tmux.kill_session(name)
    return True


def cleanup_stale_runtime(paths: AppPaths, live_session_ids: set[str]) -> list[Path]:
    """Remove runtime folders of sessions whose tmux session is gone. Ours only."""
    removed: list[Path] = []
    if not paths.runtime_dir.is_dir():
        return removed
    tmux = make_tmux(paths)
    live_names = set(tmux.sessions())
    keep = {sid[:12] for sid in live_session_ids if tmux_session_name(sid) in live_names}
    for child in paths.runtime_dir.iterdir():
        if not child.is_dir() or child.is_symlink() or child.stat().st_uid != os.getuid():
            continue
        if child.name in keep:
            continue
        sock = child / "ipc.sock"
        if sock.exists():
            import socket as pysocket

            s = pysocket.socket(pysocket.AF_UNIX)
            try:
                s.settimeout(0.5)
                s.connect(str(sock))
                s.close()
                continue  # someone is listening: not stale
            except OSError:
                pass
            finally:
                s.close()
        shutil.rmtree(child, ignore_errors=True)
        removed.append(child)
    return removed


__all__ = ["TmuxError", "WorkspaceInfo", "create_workspace"]
