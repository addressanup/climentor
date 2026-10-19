from __future__ import annotations

import logging
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from cli_mentor import apikeys
from cli_mentor.config import Config, load_config, save_config
from cli_mentor.db.store import Store, available_migrations, open_store
from cli_mentor.logs import ScrubFilter, register_secret
from cli_mentor.paths import AppPaths, UnsafeDirectoryError, ensure_private_dir
from cli_mentor.permissions import Capability, PermissionError_, PermissionManager, Scope


# ---------- paths ----------
def test_paths_respect_cli_mentor_home(mentor_env: dict[str, str]) -> None:
    paths = AppPaths.from_env()
    paths.ensure()
    assert str(paths.config_dir).startswith(mentor_env["CLI_MENTOR_HOME"])
    assert str(paths.runtime_dir) == mentor_env["CLI_MENTOR_RUNTIME_DIR"]
    for d in (paths.config_dir, paths.data_dir, paths.runtime_dir):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_paths_xdg_defaults() -> None:
    env = {"HOME": "/Users/x", "TMPDIR": "/tmp/"}
    paths = AppPaths.from_env(env)
    assert paths.config_dir == Path("/Users/x/.config/cli-mentor")
    assert paths.data_dir == Path("/Users/x/.local/share/cli-mentor")
    assert paths.runtime_dir == Path(f"/tmp/cli-mentor-{os.getuid()}")


def test_private_dir_tightens_mode(tmp_path: Path) -> None:
    d = tmp_path / "open"
    d.mkdir(mode=0o755)
    ensure_private_dir(d)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_private_dir_rejects_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(UnsafeDirectoryError):
        ensure_private_dir(link)


def test_socket_path_length_guard() -> None:
    paths = AppPaths.from_env({"HOME": "/h", "CLI_MENTOR_RUNTIME_DIR": "/tmp/" + "x" * 120})
    with pytest.raises(UnsafeDirectoryError):
        paths.socket_path("abcdef123456")


# ---------- config ----------
def test_config_roundtrip(tmp_path: Path) -> None:
    cfg = Config()
    cfg.tutor.provider = "openai"
    cfg.providers.openai.model = "some-model-id"
    path = tmp_path / "c" / "config.toml"
    save_config(cfg, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    assert "Secrets do NOT belong here" in text
    loaded = load_config(path)
    assert loaded.tutor.provider == "openai"
    assert loaded.provider_settings().model == "some-model-id"


def test_config_rejects_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('api_key = "sk-oops"\n')
    with pytest.raises(ValueError):
        load_config(path)


def test_missing_config_gives_defaults(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "none.toml")
    assert cfg.initialized is False
    assert cfg.tutor.provider == "mock"


# ---------- api keys ----------
def test_api_key_env_beats_keychain() -> None:
    apikeys.set_api_key("openai", "sk-keychainvalue1234567890")
    found = apikeys.get_api_key("openai", env={"OPENAI_API_KEY": "sk-envvalue1234567890abcd"})
    assert found.source == "env"
    found = apikeys.get_api_key("openai", env={})
    assert found.source == "keychain"
    assert "keychainvalue" not in repr(found)
    assert apikeys.delete_api_key("openai")
    assert apikeys.get_api_key("openai", env={}).source == "none"


def test_scrub_keys() -> None:
    text = "key=sk-ant-api03-ABCDEFGHIJKLMNOP and sk-proj-abcdefghijklmnopqrstu"
    cleaned = apikeys.scrub_keys(text)
    assert "ABCDEFGH" not in cleaned
    assert "abcdefghijklmnop" not in cleaned


def test_log_filter_masks_registered_secret() -> None:
    register_secret("custom-secret-value-xyz")
    record = logging.LogRecord(
        "cli_mentor", logging.INFO, __file__, 1, "using %s", ("custom-secret-value-xyz",), None
    )
    ScrubFilter().filter(record)
    assert "custom-secret-value-xyz" not in record.getMessage()


# ---------- store ----------
def test_migrations_apply_once(tmp_path: Path) -> None:
    db = tmp_path / "m.sqlite3"
    store = Store(db)
    applied = store.migrate()
    assert applied == [v for v, _, _ in available_migrations()]
    assert store.migrate() == []
    assert stat.S_IMODE(db.stat().st_mode) == 0o600
    mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    store.close()


def test_migration_upgrade_and_rollback(tmp_path: Path) -> None:
    store = open_store(tmp_path / "u.sqlite3")
    base = store.schema_version()
    good = (base + 1, f"{base + 1:04d}_add_col.sql", "ALTER TABLE sessions ADD COLUMN note TEXT;")
    assert store.migrate([good]) == [base + 1]
    bad = (base + 2, f"{base + 2:04d}_bad.sql", "CREATE TABLE x(a); SELECT nope FROM missing;")
    with pytest.raises(sqlite3.Error):
        store.migrate([good, bad])
    assert store.schema_version() == base + 1
    tables = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master")}
    assert "x" not in tables


def test_fts_search(tmp_path: Path) -> None:
    store = open_store(tmp_path / "f.sqlite3")
    store.conn.execute(
        "INSERT INTO memory_items(category, text, provenance, status, created_at, updated_at) "
        "VALUES ('note', 'prefers pipelines explained symbol by symbol', 'user_stated', 'confirmed', 0, 0)"
    )
    rows = store.conn.execute(
        "SELECT rowid FROM memory_fts WHERE memory_fts MATCH 'pipelines'"
    ).fetchall()
    assert len(rows) == 1


# ---------- permissions ----------
def test_permissions_path_scope_and_revoke(tmp_path: Path, workspace: Path) -> None:
    store = open_store(tmp_path / "p.sqlite3")
    pm = PermissionManager(store, "sess1")
    assert not pm.check(Capability.READ_FILES, workspace / "a.txt").allowed
    pm.grant(Capability.READ_FILES, Scope.SESSION, workspace)
    assert pm.check(Capability.READ_FILES, workspace / "sub" / "a.txt").allowed
    assert not pm.check(Capability.READ_FILES, tmp_path / "outside.txt").allowed
    # A grant for another session does not leak.
    assert not PermissionManager(store, "sess2").check(Capability.READ_FILES, workspace).allowed
    assert pm.revoke(Capability.READ_FILES) == 1
    assert not pm.check(Capability.READ_FILES, workspace).allowed


def test_permissions_symlink_escape(tmp_path: Path, workspace: Path) -> None:
    store = open_store(tmp_path / "p.sqlite3")
    pm = PermissionManager(store, "s")
    pm.grant(Capability.READ_FILES, Scope.SESSION, workspace)
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    (workspace / "link.txt").symlink_to(outside)
    assert not pm.check(Capability.READ_FILES, workspace / "link.txt").allowed


def test_permissions_once_and_never(tmp_path: Path, workspace: Path) -> None:
    store = open_store(tmp_path / "p.sqlite3")
    pm = PermissionManager(store, "s")
    pm.grant(Capability.RUN_LEARNER_CODE, Scope.ONCE, workspace)
    decision = pm.check(Capability.RUN_LEARNER_CODE, workspace)
    assert decision.allowed
    pm.consume(decision)
    assert not pm.check(Capability.RUN_LEARNER_CODE, workspace).allowed
    never = pm.check(Capability.INSTALL_SOFTWARE)
    assert not never.allowed and not never.needs_approval
    with pytest.raises(PermissionError_):
        pm.grant(Capability.RUN_SHELL, Scope.SESSION)


def test_persistent_grant_survives_sessions(tmp_path: Path) -> None:
    store = open_store(tmp_path / "p.sqlite3")
    PermissionManager(store, "a").grant(Capability.NETWORK, Scope.PERSISTENT, "anthropic")
    pm_b = PermissionManager(store, "b")
    assert pm_b.check(Capability.NETWORK, "anthropic").allowed
    assert not pm_b.check(Capability.NETWORK, "openai").allowed
