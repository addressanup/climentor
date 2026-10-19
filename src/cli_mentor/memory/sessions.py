"""C. Session state, transcript, and usage accounting."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field

from ..db.store import Store


@dataclass
class SessionState:
    session_id: str
    goal: str | None = None
    mode: str = "guided"
    active_exercise: str | None = None
    exercise_stage: str | None = None
    hint_level: int = 0
    summary: str | None = None
    open_questions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SessionInfo:
    id: str
    created_at: float
    updated_at: float
    ended_at: float | None
    status: str
    tmux_session: str | None
    provider: str | None
    model: str | None


class Sessions:
    def __init__(self, store: Store) -> None:
        self.store = store

    def create(self, provider: str, model: str, tmux_session: str | None = None) -> str:
        sid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        now = time.time()
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO sessions(id, created_at, updated_at, tmux_session, provider, model) VALUES (?, ?, ?, ?, ?, ?)",
                (sid, now, now, tmux_session, provider, model),
            )
            conn.execute("INSERT INTO session_state(session_id, updated_at) VALUES (?, ?)", (sid, now))
        return sid

    def get(self, sid: str) -> SessionInfo | None:
        row = self.store.conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        if row is None:
            return None
        return SessionInfo(
            row["id"],
            row["created_at"],
            row["updated_at"],
            row["ended_at"],
            row["status"],
            row["tmux_session"],
            row["provider"],
            row["model"],
        )

    def find(self, prefix: str) -> SessionInfo | None:
        rows = self.store.conn.execute("SELECT id FROM sessions WHERE id LIKE ?", (prefix + "%",)).fetchall()
        return self.get(rows[0]["id"]) if len(rows) == 1 else None

    def list_sessions(self, limit: int = 50) -> list[SessionInfo]:
        rows = self.store.conn.execute("SELECT id FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [s for s in (self.get(r["id"]) for r in rows) if s is not None]

    def latest(self) -> SessionInfo | None:
        items = self.list_sessions(1)
        return items[0] if items else None

    def touch(self, sid: str, **fields: object) -> None:
        allowed = {"tmux_session", "provider", "model"}
        sets = ["updated_at = ?"]
        params: list[object] = [time.time()]
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key} = ?")
                params.append(value)
        self.store.conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", (*params, sid))

    def end(self, sid: str) -> None:
        now = time.time()
        self.store.conn.execute(
            "UPDATE sessions SET status = 'ended', ended_at = ?, updated_at = ? WHERE id = ?",
            (now, now, sid),
        )

    def reopen(self, sid: str) -> None:
        self.store.conn.execute(
            "UPDATE sessions SET status = 'active', ended_at = NULL, updated_at = ? WHERE id = ?",
            (time.time(), sid),
        )

    # ------------------------------------------------------------ state
    def state(self, sid: str) -> SessionState:
        row = self.store.conn.execute("SELECT * FROM session_state WHERE session_id = ?", (sid,)).fetchone()
        if row is None:
            return SessionState(sid)
        return SessionState(
            sid,
            row["goal"],
            row["mode"],
            row["active_exercise"],
            row["exercise_stage"],
            row["hint_level"],
            row["summary"],
            json.loads(row["open_questions"] or "[]"),
        )

    def save_state(self, st: SessionState) -> None:
        self.store.conn.execute(
            "INSERT INTO session_state(session_id, goal, mode, active_exercise, exercise_stage, hint_level, summary, "
            "open_questions, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
            "goal = excluded.goal, mode = excluded.mode, active_exercise = excluded.active_exercise, "
            "exercise_stage = excluded.exercise_stage, hint_level = excluded.hint_level, summary = excluded.summary, "
            "open_questions = excluded.open_questions, updated_at = excluded.updated_at",
            (
                st.session_id,
                st.goal,
                st.mode,
                st.active_exercise,
                st.exercise_stage,
                st.hint_level,
                st.summary,
                json.dumps(st.open_questions),
                time.time(),
            ),
        )

    # ------------------------------------------------------------ transcript
    def add_message(
        self,
        sid: str,
        role: str,
        content: str,
        *,
        trigger: str | None = None,
        status: str = "complete",
        revision: int | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> int:
        cur = self.store.conn.execute(
            "INSERT INTO messages(session_id, role, content, trigger, status, context_revision, provider, model, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, role, content, trigger, status, revision, provider, model, time.time()),
        )
        return int(cur.lastrowid or 0)

    def recent_messages(self, sid: str, limit: int = 12) -> list[tuple[str, str, str]]:
        rows = self.store.conn.execute(
            "SELECT role, content, status FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (sid, limit),
        ).fetchall()
        return [(r["role"], r["content"], r["status"]) for r in reversed(rows)]

    # ------------------------------------------------------------ usage
    def add_usage(
        self,
        sid: str,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        estimated: bool,
    ) -> None:
        self.store.conn.execute(
            "INSERT INTO usage(session_id, provider, model, input_tokens, output_tokens, estimated, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, provider, model, input_tokens, output_tokens, 1 if estimated else 0, time.time()),
        )

    def usage_totals(self, sid: str | None = None) -> dict[str, int]:
        sql = "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), COALESCE(SUM(estimated),0), COUNT(*) FROM usage"
        params: tuple[object, ...] = ()
        if sid:
            sql += " WHERE session_id = ?"
            params = (sid,)
        row = self.store.conn.execute(sql, params).fetchone()
        return {
            "input": int(row[0]),
            "output": int(row[1]),
            "estimated_calls": int(row[2]),
            "calls": int(row[3]),
        }
