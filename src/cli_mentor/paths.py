"""Where CLI Mentor keeps its files.

We follow the XDG layout (the same one Neovim uses on macOS):

- config:  ~/.config/cli-mentor/          (settings, no secrets)
- data:    ~/.local/share/cli-mentor/     (SQLite database)
- state:   ~/.local/state/cli-mentor/     (logs)
- cache:   ~/.cache/cli-mentor/
- runtime: $XDG_RUNTIME_DIR or $TMPDIR/cli-mentor-<uid>/  (sockets, tokens)

Set ``CLI_MENTOR_HOME`` to put everything under one folder (tests and demos).
"""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

APP = "cli-mentor"
# macOS limits Unix socket paths to 104 bytes (including the trailing NUL).
MAX_SOCKET_PATH = 100


class UnsafeDirectoryError(RuntimeError):
    """A directory we rely on for privacy is owned by someone else or is too open."""


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (mode 0700) or verify an existing one is ours and private."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise UnsafeDirectoryError(f"{path} is not a real directory")
    if info.st_uid != os.getuid():
        raise UnsafeDirectoryError(f"{path} is owned by another user")
    if info.st_mode & 0o077:
        path.chmod(0o700)
    return path


def write_private_file(path: Path, data: str) -> None:
    """Write a file that only the current user can read (0600)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(data)
    path.chmod(0o600)


@dataclass(frozen=True)
class AppPaths:
    config_dir: Path
    data_dir: Path
    state_dir: Path
    cache_dir: Path
    runtime_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def db_file(self) -> Path:
        return self.data_dir / "mentor.sqlite3"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "mentor.log"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AppPaths:
        env = os.environ if env is None else env
        home_override = env.get("CLI_MENTOR_HOME")
        if home_override:
            base = Path(home_override).expanduser()
            return cls(
                config_dir=base / "config",
                data_dir=base / "data",
                state_dir=base / "state",
                cache_dir=base / "cache",
                runtime_dir=_runtime_base(env, base / "run"),
            )
        home = Path(env.get("HOME", str(Path.home())))

        def xdg(var: str, default: Path) -> Path:
            value = env.get(var)
            return Path(value) / APP if value else default / APP

        return cls(
            config_dir=xdg("XDG_CONFIG_HOME", home / ".config"),
            data_dir=xdg("XDG_DATA_HOME", home / ".local" / "share"),
            state_dir=xdg("XDG_STATE_HOME", home / ".local" / "state"),
            cache_dir=xdg("XDG_CACHE_HOME", home / ".cache"),
            runtime_dir=_runtime_base(env, None),
        )

    def ensure(self) -> None:
        for d in (self.config_dir, self.data_dir, self.state_dir, self.log_dir, self.cache_dir):
            ensure_private_dir(d)
        ensure_private_dir(self.runtime_dir)

    def session_runtime_dir(self, session_id: str) -> Path:
        return ensure_private_dir(self.runtime_dir / session_id[:12])

    def socket_path(self, session_id: str) -> Path:
        path = self.runtime_dir / session_id[:12] / "ipc.sock"
        if len(str(path).encode()) > MAX_SOCKET_PATH:
            raise UnsafeDirectoryError(
                f"Socket path is too long for macOS ({len(str(path))} bytes): {path}. "
                "Set CLI_MENTOR_RUNTIME_DIR to a shorter private folder."
            )
        return path


def _runtime_base(env: Mapping[str, str], fallback: Path | None) -> Path:
    explicit = env.get("CLI_MENTOR_RUNTIME_DIR")
    if explicit:
        return Path(explicit).expanduser()
    xdg_runtime = env.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        return Path(xdg_runtime) / APP
    tmp = env.get("TMPDIR") or tempfile.gettempdir()
    candidate = Path(tmp) / f"{APP}-{os.getuid()}"
    if fallback is not None and len(str(fallback)) + 30 < MAX_SOCKET_PATH:
        return fallback
    return candidate
