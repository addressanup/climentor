"""SQLite connection and migrations.

- WAL mode (readers do not block the writer).
- Numbered ``migrations/NNNN_name.sql`` files applied once, in order, each in
  its own transaction, recorded in ``schema_migrations``.
- The database file is created with 0600 permissions.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def available_migrations() -> list[tuple[int, str, str]]:
    folder = resources.files("cli_mentor.db").joinpath("migrations")
    found: list[tuple[int, str, str]] = []
    for entry in folder.iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match:
            found.append((int(match.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    return sorted(found)


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path) if str(path) != ":memory:" else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
        self.conn = sqlite3.connect(
            str(path), isolation_level=None, check_same_thread=False, timeout=5.0
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if self.path is not None:
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def schema_version(self) -> int:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at REAL NOT NULL)"
        )
        row = self.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)

    def migrate(self, migrations: list[tuple[int, str, str]] | None = None) -> list[int]:
        """Apply pending migrations. Returns the versions applied."""
        current = self.schema_version()
        applied: list[int] = []
        for version, name, sql in migrations or available_migrations():
            if version <= current:
                continue
            # executescript() commits implicitly, so wrap the script ourselves.
            try:
                self.conn.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + sql
                    + f"\nINSERT INTO schema_migrations(version, name, applied_at) "
                    f"VALUES ({version}, '{name}', {time.time()});\nCOMMIT;"
                )
            except sqlite3.Error:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise
            applied.append(version)
        return applied

    def checkpoint_and_vacuum(self) -> None:
        """Fold the WAL back into the main file and rebuild it without free pages."""
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.execute("VACUUM")
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def open_store(path: Path) -> Store:
    store = Store(path)
    store.migrate()
    return store
