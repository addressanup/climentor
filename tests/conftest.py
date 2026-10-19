"""Shared test fixtures.

Every test runs with temp XDG folders, a short private runtime folder, and an
in-memory keyring, so the learner's real data, Keychain, and tmux server are
never touched.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend

# Subprocesses started by tests must never reach the real Keychain either.
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
for _var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(_var, None)


class MemoryKeyring(KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop((service, username), None)


@pytest.fixture(autouse=True)
def memory_keyring() -> Iterator[MemoryKeyring]:
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend


@pytest.fixture
def short_tmp() -> Iterator[Path]:
    """A short temp dir (macOS socket paths must stay under 104 bytes)."""
    path = Path(tempfile.mkdtemp(prefix="cm-", dir="/tmp")).resolve()
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def mentor_env(tmp_path: Path, short_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    home = tmp_path / "mentor-home"
    env = {
        "CLI_MENTOR_HOME": str(home),
        "CLI_MENTOR_RUNTIME_DIR": str(short_tmp / "run"),
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = (tmp_path / "ws").resolve()
    root.mkdir()
    return root


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        pytest.skip(f"{name} is not installed")
    return path
