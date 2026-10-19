"""E. Free-form memory items with provenance, FTS search, and corrections.

Model inferences are stored as ``proposed`` and never silently become facts:
only ``confirmed`` items are presented to the model as established.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass

from ..db.store import Store


@dataclass(frozen=True)
class MemoryItem:
    id: int
    category: str
    text: str
    provenance: str
    status: str
    source: str | None
    created_at: float


def _row(r: sqlite3.Row) -> MemoryItem:
    return MemoryItem(
        r["id"],
        r["category"],
        r["text"],
        r["provenance"],
        r["status"],
        r["source"],
        r["created_at"],
    )


class Notes:
    def __init__(self, store: Store) -> None:
        self.store = store

    def add(
        self,
        text: str,
        *,
        category: str = "note",
        provenance: str = "user_stated",
        status: str | None = None,
        source: str | None = None,
        session_id: str | None = None,
    ) -> int:
        if status is None:
            status = "proposed" if provenance == "model_inferred" else "confirmed"
        now = time.time()
        cur = self.store.conn.execute(
            "INSERT INTO memory_items(category, text, provenance, status, source, session_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (category, text.strip()[:2000], provenance, status, source, session_id, now, now),
        )
        return int(cur.lastrowid or 0)

    def get(self, item_id: int) -> MemoryItem | None:
        row = self.store.conn.execute("SELECT * FROM memory_items WHERE id = ?", (item_id,)).fetchone()
        return _row(row) if row else None

    def items(self, status: str | None = None, limit: int = 100) -> list[MemoryItem]:
        sql = "SELECT * FROM memory_items"
        params: list[object] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        else:
            sql += " WHERE status IN ('confirmed', 'proposed')"
        rows = self.store.conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
        return [_row(r) for r in rows]

    def search(self, query: str, limit: int = 20) -> list[MemoryItem]:
        terms = re.findall(r"[\w]+", query)
        if not terms:
            return []
        fts = " OR ".join(f'"{t}"' for t in terms)
        rows = self.store.conn.execute(
            "SELECT m.* FROM memory_fts f JOIN memory_items m ON m.id = f.rowid "
            "WHERE memory_fts MATCH ? AND m.status IN ('confirmed', 'proposed') ORDER BY rank LIMIT ?",
            (fts, limit),
        ).fetchall()
        return [_row(r) for r in rows]

    def set_status(self, item_id: int, status: str) -> bool:
        return (
            self.store.conn.execute(
                "UPDATE memory_items SET status = ?, updated_at = ? WHERE id = ?",
                (status, time.time(), item_id),
            ).rowcount
            > 0
        )

    def confirm(self, item_id: int) -> bool:
        return self.set_status(item_id, "confirmed")

    def reject(self, item_id: int) -> bool:
        return self.set_status(item_id, "rejected")

    def correct(self, item_id: int, new_text: str) -> int | None:
        old = self.get(item_id)
        if old is None:
            return None
        new_id = self.add(
            new_text,
            category=old.category,
            provenance="user_stated",
            source=f"correction of #{item_id}",
        )
        self.store.conn.execute(
            "UPDATE memory_items SET status = 'superseded', superseded_by = ?, updated_at = ? WHERE id = ?",
            (new_id, time.time(), item_id),
        )
        return new_id

    def forget(self, item_id: int) -> bool:
        return self.store.conn.execute("DELETE FROM memory_items WHERE id = ?", (item_id,)).rowcount > 0
