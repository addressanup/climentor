"""Session-scoped zsh integration.

We start the shell pane with ``ZDOTDIR`` pointing at a private folder holding
four tiny wrapper files. Each one loads the learner's real file of the same
name (unchanged), and ``.zshrc`` also loads our hooks. At the end the original
``ZDOTDIR`` is restored so child shells behave normally. Nothing in the
learner's home folder is modified.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from ...paths import ensure_private_dir

HOOKS_FILE = "mentor-hooks.zsh"


def hooks_path() -> Path:
    return Path(str(resources.files("cli_mentor.observe.shell").joinpath(HOOKS_FILE)))


_HEADER = "# CLI Mentor session wrapper (temporary). Your own file is loaded unchanged.\n"

_ZSHENV = (
    _HEADER
    + """_mentor_zdotdir=$ZDOTDIR
ZDOTDIR=${CLI_MENTOR_USER_ZDOTDIR:-$HOME}
[[ -f $ZDOTDIR/.zshenv ]] && source $ZDOTDIR/.zshenv
_mentor_user_zdotdir=$ZDOTDIR
ZDOTDIR=$_mentor_zdotdir
"""
)

_ZPROFILE = (
    _HEADER
    + """ZDOTDIR=$_mentor_user_zdotdir
[[ -f $ZDOTDIR/.zprofile ]] && source $ZDOTDIR/.zprofile
ZDOTDIR=$_mentor_zdotdir
"""
)

_RESTORE = """if [[ -n ${CLI_MENTOR_USER_ZDOTDIR:-} ]]; then
  ZDOTDIR=$_mentor_user_zdotdir
else
  unset ZDOTDIR
fi
unset _mentor_zdotdir _mentor_user_zdotdir CLI_MENTOR_USER_ZDOTDIR
"""

_ZSHRC_TEMPLATE = (
    _HEADER
    + """ZDOTDIR=$_mentor_user_zdotdir
[[ -f $ZDOTDIR/.zshrc ]] && source $ZDOTDIR/.zshrc
ZDOTDIR=$_mentor_zdotdir
source {hooks}
[[ -o login ]] || {{
{restore}}}
"""
)

_ZLOGIN = (
    _HEADER
    + """ZDOTDIR=$_mentor_user_zdotdir
[[ -f $ZDOTDIR/.zlogin ]] && source $ZDOTDIR/.zlogin
"""
    + _RESTORE
)


def _zsh_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"


def prepare_zdotdir(session_dir: Path) -> Path:
    """Write the wrapper files into ``session_dir/zdotdir`` and return that folder."""
    zdot = ensure_private_dir(session_dir / "zdotdir")
    files = {
        ".zshenv": _ZSHENV,
        ".zprofile": _ZPROFILE,
        ".zshrc": _ZSHRC_TEMPLATE.format(hooks=_zsh_quote(hooks_path()), restore=_RESTORE),
        ".zlogin": _ZLOGIN,
    }
    for name, content in files.items():
        target = zdot / name
        target.write_text(content, encoding="utf-8")
        target.chmod(0o600)
    return zdot


def shell_environment(
    *,
    zdotdir: Path,
    socket_path: Path,
    token_file: Path,
    nvim_rtp: Path | None,
    user_zdotdir: str | None,
) -> dict[str, str]:
    """Environment variables for the shell pane (no secret values, only paths)."""
    env = {
        "ZDOTDIR": str(zdotdir),
        "CLI_MENTOR_SOCK": str(socket_path),
        "CLI_MENTOR_TOKEN_FILE": str(token_file),
        "CLI_MENTOR_USER_ZDOTDIR": user_zdotdir or "",
    }
    if nvim_rtp is not None:
        env["CLI_MENTOR_NVIM_RTP"] = str(nvim_rtp)
    return env
