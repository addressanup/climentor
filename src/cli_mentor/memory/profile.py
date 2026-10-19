"""A. Learner profile: explicit goals, preferences, constraints.

Defaults are seeded as *proposed*. Only facts the learner confirms (or states
directly) are treated as established and sent to the model as such.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..db.store import Store

DEFAULT_PROFILE: list[tuple[str, str]] = [
    ("platform", "Uses an Apple Silicon Mac."),
    ("goal.breadth", "Wants beginner-to-advanced software engineering and programming education."),
    ("goal.cli_nvim", "Wants to become a proficient CLI and Neovim user."),
    (
        "style.project_based",
        "Prefers practical, project-based learning with progressive conceptual depth.",
    ),
    (
        "style.syntax_word_by_word",
        "Needs unfamiliar syntax explained word by word and symbol by symbol.",
    ),
    (
        "style.no_assumed_knowledge",
        "Does not want the tutor to assume knowledge it has not established.",
    ),
    ("style.continuity", "Wants personalization and continuity between sessions."),
    ("style.do_the_work", "Wants to do the work personally rather than have the agent do it."),
]

INTERVENTION_KEY = "pref.intervention"
INTERVENTION_CHOICES = ("quiet", "balanced", "proactive")


@dataclass(frozen=True)
class ProfileFact:
    key: str
    value: str
    provenance: str
    status: str
    updated_at: float


class Profile:
    def __init__(self, store: Store) -> None:
        self.store = store

    def seed_defaults(self) -> int:
        now = time.time()
        added = 0
        for key, value in DEFAULT_PROFILE:
            cur = self.store.conn.execute(
                "INSERT OR IGNORE INTO profile_facts(key, value, provenance, status, source, created_at, updated_at) "
                "VALUES (?, ?, 'default_proposed', 'proposed', 'onboarding defaults', ?, ?)",
                (key, value, now, now),
            )
            added += cur.rowcount
        return added

    def facts(self, include_rejected: bool = False) -> list[ProfileFact]:
        sql = "SELECT key, value, provenance, status, updated_at FROM profile_facts"
        if not include_rejected:
            sql += " WHERE status != 'rejected'"
        rows = self.store.conn.execute(sql + " ORDER BY id").fetchall()
        return [ProfileFact(r["key"], r["value"], r["provenance"], r["status"], r["updated_at"]) for r in rows]

    def confirmed(self) -> list[ProfileFact]:
        return [f for f in self.facts() if f.status == "confirmed"]

    def get(self, key: str) -> ProfileFact | None:
        return next((f for f in self.facts(include_rejected=True) if f.key == key), None)

    def _status(self, key: str, status: str) -> bool:
        cur = self.store.conn.execute(
            "UPDATE profile_facts SET status = ?, updated_at = ? WHERE key = ?",
            (status, time.time(), key),
        )
        return cur.rowcount > 0

    def confirm(self, key: str) -> bool:
        return self._status(key, "confirmed")

    def reject(self, key: str) -> bool:
        return self._status(key, "rejected")

    def confirm_all_proposed(self) -> int:
        cur = self.store.conn.execute(
            "UPDATE profile_facts SET status = 'confirmed', updated_at = ? WHERE status = 'proposed' "
            "AND provenance = 'default_proposed'",
            (time.time(),),
        )
        return cur.rowcount

    def set(self, key: str, value: str) -> None:
        """A learner-stated fact (confirmed by definition)."""
        now = time.time()
        self.store.conn.execute(
            "INSERT INTO profile_facts(key, value, provenance, status, source, created_at, updated_at) "
            "VALUES (?, ?, 'user_stated', 'confirmed', 'learner', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, provenance = 'user_stated', "
            "status = 'confirmed', source = 'learner', updated_at = excluded.updated_at",
            (key, value.strip(), now, now),
        )

    def remove(self, key: str) -> bool:
        return self.store.conn.execute("DELETE FROM profile_facts WHERE key = ?", (key,)).rowcount > 0

    def intervention_style(self) -> str:
        fact = self.get(INTERVENTION_KEY)
        if fact and fact.status == "confirmed" and fact.value in INTERVENTION_CHOICES:
            return fact.value
        return "balanced"
