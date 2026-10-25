"""B. Skill evidence, assistance levels, skill states, and review scheduling.

Skill states (no "mastery %" — we do not have a model that justifies one):
  introduced -> practiced_with_assistance -> demonstrated_independently
  -> demonstrated_new_context
"Due for review" is computed from ``next_review_at``.

Review intervals are a simple heuristic (1, 3, 7, 14, 30 days), not a
validated memory model. Evidence only comes from trusted checks, the
learner's own answers, or (clearly labelled) model inferences.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from ..curriculum.model import Lesson
from ..curriculum.verify import VerificationResult
from ..db.store import Store

ASSISTANCE_LABELS = {
    0: "none",
    1: "nudge",
    2: "specific hint",
    3: "worked example",
    4: "full solution",
}
STATE_ORDER = [
    "introduced",
    "practiced_with_assistance",
    "demonstrated_independently",
    "demonstrated_new_context",
]
REVIEW_INTERVALS_DAYS = [1.0, 3.0, 7.0, 14.0, 30.0]
DAY = 86400.0


@dataclass(frozen=True)
class Attempt:
    id: str
    exercise_id: str
    status: str
    max_assistance: int
    hints_used: int
    solution_shown: bool
    mode: str
    started_at: float


@dataclass(frozen=True)
class SkillState:
    skill: str
    state: str
    independent_passes: int
    assisted_passes: int
    failures: int
    contexts: list[str]
    next_review_at: float | None

    def due(self, now: float) -> bool:
        return self.next_review_at is not None and self.next_review_at <= now


def _higher(a: str, b: str) -> str:
    return a if STATE_ORDER.index(a) >= STATE_ORDER.index(b) else b


class EvidenceStore:
    def __init__(self, store: Store, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.clock = clock

    # ------------------------------------------------------------ attempts
    def _attempt(self, r: sqlite3.Row) -> Attempt:
        return Attempt(
            r["id"],
            r["exercise_id"],
            r["status"],
            r["max_assistance"],
            r["hints_used"],
            bool(r["solution_shown"]),
            r["mode"],
            r["started_at"],
        )

    def current_attempt(self, exercise_id: str) -> Attempt | None:
        row = self.store.conn.execute(
            "SELECT * FROM attempts WHERE exercise_id = ? AND status = 'in_progress' ORDER BY started_at DESC LIMIT 1",
            (exercise_id,),
        ).fetchone()
        return self._attempt(row) if row else None

    def get_attempt(self, attempt_id: str) -> Attempt | None:
        row = self.store.conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        return self._attempt(row) if row else None

    def latest_attempt(self, exercise_id: str) -> Attempt | None:
        row = self.store.conn.execute(
            "SELECT * FROM attempts WHERE exercise_id = ? ORDER BY started_at DESC LIMIT 1", (exercise_id,)
        ).fetchone()
        return self._attempt(row) if row else None

    def start_attempt(self, session_id: str | None, lesson: Lesson, mode: str) -> Attempt:
        existing = self.current_attempt(lesson.id)
        if existing is not None:
            return existing
        attempt_id = uuid.uuid4().hex
        self.store.conn.execute(
            "INSERT INTO attempts(id, session_id, exercise_id, exercise_version, mode, status, started_at) "
            "VALUES (?, ?, ?, ?, ?, 'in_progress', ?)",
            (attempt_id, session_id, lesson.id, lesson.version, mode, self.clock()),
        )
        attempt = self.get_attempt(attempt_id)
        assert attempt is not None
        return attempt

    def record_assistance(self, attempt_id: str, level: int, *, hint: bool = False, solution: bool = False) -> None:
        self.store.conn.execute(
            "UPDATE attempts SET max_assistance = MAX(max_assistance, ?), "
            "hints_used = hints_used + ?, solution_shown = MAX(solution_shown, ?) WHERE id = ?",
            (level, 1 if hint else 0, 1 if solution else 0, attempt_id),
        )

    def close_attempt(self, attempt_id: str, status: str = "passed") -> None:
        self.store.conn.execute(
            "UPDATE attempts SET status = ?, finished_at = ? WHERE id = ? AND status = 'in_progress'",
            (status, self.clock(), attempt_id),
        )

    def abandon(self, attempt_id: str) -> None:
        self.store.conn.execute(
            "UPDATE attempts SET status = 'abandoned', finished_at = ? WHERE id = ? AND status = 'in_progress'",
            (self.clock(), attempt_id),
        )

    # ------------------------------------------------------------ evidence
    def introduce(self, skills: list[str]) -> None:
        now = self.clock()
        for skill in skills:
            self.store.conn.execute(
                "INSERT OR IGNORE INTO skill_state(skill, state, updated_at) VALUES (?, 'introduced', ?)",
                (skill, now),
            )

    def record_verification(
        self, attempt_id: str, lesson: Lesson, result: VerificationResult, *, understanding: bool = False
    ) -> None:
        attempt = self.get_attempt(attempt_id)
        if attempt is None:
            raise KeyError(attempt_id)
        now = self.clock()
        details = json.dumps(
            {
                "verifier": result.verifier,
                "checks": [{"name": c.name, "passed": c.passed, "evidence": c.evidence} for c in result.checks],
                "technique_detail": result.technique_detail,
                "executed_learner_code": result.executed_learner_code,
                "hints_used": attempt.hints_used,
                "solution_shown": attempt.solution_shown,
                "mode": attempt.mode,
            }
        )
        with self.store.tx() as conn:
            for skill in lesson.skills:
                conn.execute(
                    "INSERT INTO skill_evidence(skill, attempt_id, exercise_id, kind, result, assistance_level, "
                    "technique, provenance, details, created_at) VALUES (?, ?, ?, 'verification', ?, ?, ?, 'observed', ?, ?)",
                    (
                        skill,
                        attempt_id,
                        lesson.id,
                        "pass" if result.passed else "fail",
                        attempt.max_assistance,
                        result.technique,
                        details,
                        now,
                    ),
                )
            if result.passed:
                conn.execute(
                    "UPDATE attempts SET status = 'passed', finished_at = ? WHERE id = ?",
                    (now, attempt_id),
                )
        for skill in lesson.skills:
            if not result.passed or understanding or attempt.max_assistance:
                self._update_state(skill, lesson.id, result.passed, attempt.max_assistance, review=False)
            else:
                self.introduce([skill])  # a successful run alone does not demonstrate understanding

    def record_understanding(
        self, attempt_id: str, lesson: Lesson, text: str, *, independent: bool, transfer: bool = False
    ) -> None:
        """A semantic judgment stays model-inferred, distinct from trusted execution evidence."""
        if not text.strip():
            return
        attempt = self.get_attempt(attempt_id)
        if attempt is None:
            raise KeyError(attempt_id)
        assistance = attempt.max_assistance
        for skill in lesson.skills:
            self.store.conn.execute(
                "INSERT INTO skill_evidence(skill, attempt_id, exercise_id, kind, result, assistance_level, "
                "technique, provenance, details, created_at) "
                "VALUES (?, ?, ?, ?, 'unverified', ?, 'not_applicable', 'model_inferred', ?, ?)",
                (
                    skill,
                    attempt_id,
                    lesson.id,
                    "transfer" if transfer else "explanation",
                    assistance,
                    json.dumps({"answer": text[:1000], "independent": independent and assistance == 0}),
                    self.clock(),
                ),
            )
            if independent or assistance:
                self._update_state(skill, lesson.id, True, assistance, review=False)
            else:
                self.introduce([skill])  # recall alone proves neither independent use nor assistance

    def record_answer(
        self,
        attempt_id: str | None,
        lesson: Lesson,
        kind: str,
        text: str,
        provenance: str = "user_stated",
    ) -> None:
        """A prediction or transfer answer. Stored as unverified evidence."""
        if not text.strip():
            return
        now = self.clock()
        for skill in lesson.skills:
            self.store.conn.execute(
                "INSERT INTO skill_evidence(skill, attempt_id, exercise_id, kind, result, assistance_level, "
                "technique, provenance, details, created_at) VALUES (?, ?, ?, ?, 'unverified', 0, 'not_applicable', ?, ?, ?)",
                (
                    skill,
                    attempt_id,
                    lesson.id,
                    kind,
                    provenance,
                    json.dumps({"answer": text[:1000]}),
                    now,
                ),
            )

    def record_review(self, skill: str, passed: bool, assistance: int, exercise_id: str | None = None) -> None:
        self.store.conn.execute(
            "INSERT INTO skill_evidence(skill, exercise_id, kind, result, assistance_level, provenance, created_at) "
            "VALUES (?, ?, 'review', ?, ?, 'observed', ?)",
            (skill, exercise_id, "pass" if passed else "fail", assistance, self.clock()),
        )
        self._update_state(skill, exercise_id or "review", passed, assistance, review=True)

    def _update_state(self, skill: str, context: str, passed: bool, assistance: int, review: bool) -> None:
        now = self.clock()
        row = self.store.conn.execute("SELECT * FROM skill_state WHERE skill = ?", (skill,)).fetchone()
        state = row["state"] if row else "introduced"
        independent = row["independent_passes"] if row else 0
        assisted = row["assisted_passes"] if row else 0
        failures = row["failures"] if row else 0
        contexts: list[str] = json.loads(row["contexts"]) if row else []
        interval = row["review_interval_days"] if row else None
        next_review = row["next_review_at"] if row else None
        if not passed:
            failures += 1
            if review or interval is not None:
                interval = REVIEW_INTERVALS_DAYS[0]
                next_review = now + interval * DAY
        elif assistance > 0:
            assisted += 1
            state = _higher(state, "practiced_with_assistance")
            interval = REVIEW_INTERVALS_DAYS[0]
            next_review = now + interval * DAY
        else:
            independent += 1
            if context not in contexts:
                contexts.append(context)
            state = _higher(state, "demonstrated_independently")
            if len(contexts) >= 2:
                state = _higher(state, "demonstrated_new_context")
            if interval is None:
                interval = REVIEW_INTERVALS_DAYS[0]
            else:
                bigger = [d for d in REVIEW_INTERVALS_DAYS if d > interval]
                interval = bigger[0] if bigger else REVIEW_INTERVALS_DAYS[-1]
            next_review = now + interval * DAY
        self.store.conn.execute(
            "INSERT INTO skill_state(skill, state, independent_passes, assisted_passes, failures, contexts, "
            "review_interval_days, next_review_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(skill) DO UPDATE SET state = excluded.state, independent_passes = excluded.independent_passes, "
            "assisted_passes = excluded.assisted_passes, failures = excluded.failures, contexts = excluded.contexts, "
            "review_interval_days = excluded.review_interval_days, next_review_at = excluded.next_review_at, "
            "updated_at = excluded.updated_at",
            (
                skill,
                state,
                independent,
                assisted,
                failures,
                json.dumps(contexts),
                interval,
                next_review,
                now,
            ),
        )

    # ------------------------------------------------------------ queries
    def skill_states(self) -> list[SkillState]:
        rows = self.store.conn.execute("SELECT * FROM skill_state ORDER BY skill").fetchall()
        return [
            SkillState(
                r["skill"],
                r["state"],
                r["independent_passes"],
                r["assisted_passes"],
                r["failures"],
                json.loads(r["contexts"]),
                r["next_review_at"],
            )
            for r in rows
        ]

    def due_reviews(self) -> list[SkillState]:
        now = self.clock()
        return [s for s in self.skill_states() if s.due(now)]

    def completed_exercises(self) -> set[str]:
        rows = self.store.conn.execute("SELECT DISTINCT exercise_id FROM attempts WHERE status = 'passed'").fetchall()
        return {r[0] for r in rows}

    def evidence_for(self, skills: list[str], limit: int = 20) -> list[dict[str, object]]:
        if not skills:
            return []
        marks = ",".join("?" * len(skills))
        rows = self.store.conn.execute(
            f"SELECT skill, exercise_id, kind, result, assistance_level, technique, provenance, created_at "
            f"FROM skill_evidence WHERE skill IN ({marks}) ORDER BY created_at DESC LIMIT ?",
            (*skills, limit),
        ).fetchall()
        return [dict(r) for r in rows]
