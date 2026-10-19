"""Retention, export, and deletion.

Deletion removes rows, then folds the SQLite write-ahead log back into the
database and rebuilds the file (VACUUM) so deleted pages are not kept around.
This is NOT forensic secure erasure: copies may survive in backups (for
example Time Machine), in filesystem snapshots, or on the SSD itself, and any
data already sent to a model provider cannot be recalled.
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..config import Config
from ..db.store import Store

EXPORT_TABLES = [
    "profile_facts",
    "memory_items",
    "sessions",
    "session_state",
    "attempts",
    "skill_evidence",
    "skill_state",
    "project_memory",
    "permissions",
    "usage",
]


def purge_expired(store: Store, config: Config, now: float | None = None) -> dict[str, int]:
    now = now or time.time()
    ev_cut = now - config.retention.raw_events_days * 86400
    msg_cut = now - config.retention.transcripts_days * 86400
    events = store.conn.execute("DELETE FROM events WHERE received_at < ?", (ev_cut,)).rowcount
    messages = store.conn.execute("DELETE FROM messages WHERE created_at < ?", (msg_cut,)).rowcount
    return {"events": events, "messages": messages}


def export_all(store: Store, include_transcripts: bool = True, include_events: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "format": "cli-mentor-export",
        "version": 1,
        "exported_at": time.time(),
        "note": "No API keys or secrets are included. Observed text was redacted before storage (best effort).",
    }
    tables = list(EXPORT_TABLES)
    if include_transcripts:
        tables.append("messages")
    if include_events:
        tables.append("events")
    for table in tables:
        rows = store.conn.execute(f"SELECT * FROM {table}").fetchall()
        out[table] = [dict(r) for r in rows]
    return out


def export_json(store: Store, **kw: bool) -> str:
    return json.dumps(export_all(store, **kw), indent=2, default=str)


def delete_session(store: Store, session_id: str, with_evidence: bool = False) -> dict[str, int]:
    counts: dict[str, int] = {}
    with store.tx() as conn:
        counts["events"] = conn.execute("DELETE FROM events WHERE session_id = ?", (session_id,)).rowcount
        counts["messages"] = conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,)).rowcount
        # Things the learner said themselves are kept; observations and model
        # suggestions from this session are removed.
        counts["memory_items"] = conn.execute(
            "DELETE FROM memory_items WHERE session_id = ? AND provenance != 'user_stated'",
            (session_id,),
        ).rowcount
        counts["usage"] = conn.execute("DELETE FROM usage WHERE session_id = ?", (session_id,)).rowcount
        counts["permissions"] = conn.execute(
            "DELETE FROM permissions WHERE session_id = ? AND scope != 'persistent'", (session_id,)
        ).rowcount
        if with_evidence:
            counts["attempts"] = conn.execute("DELETE FROM attempts WHERE session_id = ?", (session_id,)).rowcount
        counts["sessions"] = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,)).rowcount
    store.checkpoint_and_vacuum()
    return counts


def delete_observations(store: Store, session_id: str | None = None) -> int:
    if session_id:
        n = store.conn.execute("DELETE FROM events WHERE session_id = ?", (session_id,)).rowcount
    else:
        n = store.conn.execute("DELETE FROM events").rowcount
    store.checkpoint_and_vacuum()
    return n
