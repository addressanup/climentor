from __future__ import annotations

import json
from pathlib import Path

from cli_mentor.config import Config
from cli_mentor.curriculum.model import Hint, Lesson
from cli_mentor.curriculum.verify import Check, VerificationResult
from cli_mentor.db.store import open_store
from cli_mentor.memory.evidence import DAY, EvidenceStore
from cli_mentor.memory.notes import Notes
from cli_mentor.memory.privacy_ops import delete_session, export_json, purge_expired
from cli_mentor.memory.profile import DEFAULT_PROFILE, Profile
from cli_mentor.memory.sessions import Sessions
from cli_mentor.tutor.policy import InterventionPolicy, PolicySettings, Trigger


def make_lesson(lid: str, skills: list[str]) -> Lesson:
    return Lesson(
        id=lid,
        version=1,
        title=lid,
        track="plan",
        skills=skills,
        objectives=["x"],
        instructions="do the thing",
        workdir="plans/x",
        hints=[Hint(level=1, text="h")],
        verifier="plan.generic.v1",
        risk="writes_workspace",
    )


NAV = make_lesson("plan.p00000001.s1", ["cli.navigate.pwd", "cli.navigate.cd"])
FILES = make_lesson("plan.p00000001.s2", ["cli.files.cp"])


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def decide(p: InterventionPolicy, trigger: Trigger = Trigger.REPEATED_FAILURE, **kw: object) -> object:
    args: dict[str, object] = dict(
        mode="guided", response_in_flight=False, shell_busy=False, seconds_since_activity=10.0, exercise_active=True
    )
    args.update(kw)
    return p.decide(trigger, **args)  # type: ignore[arg-type]


# ---------- policy ----------
def test_failure_threshold_counts_only_relevant_and_resets_on_success() -> None:
    p = InterventionPolicy(PolicySettings(failure_threshold=2))
    assert not p.note_command(1, True, "a")
    assert not p.note_command(1, False, "unrelated")  # outside the exercise: ignored
    assert p.note_command(1, True, "b")
    p.note_command(0, True, "ok")
    assert p.consecutive_failures == 0


def test_solicited_always_acts_even_in_quiet_mode() -> None:
    p = InterventionPolicy(PolicySettings())
    for trig in (Trigger.HINT, Trigger.LEARNER_MESSAGE, Trigger.EXPLAIN):
        d = decide(p, trig, mode="quiet", response_in_flight=True)
        assert d.act  # type: ignore[attr-defined]


def test_unsolicited_rules() -> None:
    clock = Clock()
    p = InterventionPolicy(PolicySettings(cooldown=60, debounce=2), clock)
    assert not decide(p, mode="quiet").act  # type: ignore[attr-defined]
    assert not decide(p, mode="assessment").act  # type: ignore[attr-defined]
    assert not decide(p, style="quiet").act  # type: ignore[attr-defined]
    assert not decide(p, exercise_active=False).act  # type: ignore[attr-defined]
    assert not decide(p, response_in_flight=True).act  # type: ignore[attr-defined]
    busy = decide(p, shell_busy=True)
    assert not busy.act and busy.wait == 2  # type: ignore[attr-defined]
    typing = decide(p, seconds_since_activity=0.5)
    assert not typing.act and typing.wait == 1.5  # type: ignore[attr-defined]
    assert decide(p).act  # type: ignore[attr-defined]
    p.mark_unsolicited()
    assert not decide(p).act  # type: ignore[attr-defined]  (cooldown)
    clock.t += 61
    assert decide(p).act  # type: ignore[attr-defined]


# ---------- profile ----------
def test_profile_defaults_are_proposed_until_confirmed(tmp_path: Path) -> None:
    store = open_store(tmp_path / "db.sqlite3")
    prof = Profile(store)
    assert prof.seed_defaults() == len(DEFAULT_PROFILE)
    assert prof.seed_defaults() == 0
    assert prof.confirmed() == []
    prof.confirm("platform")
    prof.reject("style.project_based")
    prof.set("goal.custom", "Learn Rust later")
    confirmed = {f.key: f for f in prof.confirmed()}
    assert set(confirmed) == {"platform", "goal.custom"}
    assert confirmed["goal.custom"].provenance == "user_stated"
    assert prof.get("style.project_based").status == "rejected"  # type: ignore[union-attr]
    assert prof.intervention_style() == "balanced"
    prof.set("pref.intervention", "quiet")
    assert prof.intervention_style() == "quiet"


# ---------- notes ----------
def test_notes_provenance_correction_search(tmp_path: Path) -> None:
    notes = Notes(open_store(tmp_path / "db.sqlite3"))
    inferred = notes.add("Struggles with pipes", provenance="model_inferred")
    assert notes.get(inferred).status == "proposed"  # type: ignore[union-attr]
    stated = notes.add("Prefers examples with real files")
    assert notes.get(stated).status == "confirmed"  # type: ignore[union-attr]
    new_id = notes.correct(inferred, "Learning pipes, making progress")
    assert notes.get(inferred).status == "superseded"  # type: ignore[union-attr]
    assert notes.get(new_id).provenance == "user_stated"  # type: ignore[arg-type,union-attr]
    assert [i.id for i in notes.search("pipes progress")] == [new_id]
    assert notes.forget(stated) and notes.get(stated) is None


# ---------- evidence and reviews ----------
def result(passed: bool) -> VerificationResult:
    return VerificationResult("x", passed, [Check("c", passed, "", "filesystem")])


def test_skill_states_and_review_schedule(tmp_path: Path) -> None:
    clock = Clock(1_000_000.0)
    ev = EvidenceStore(open_store(tmp_path / "db.sqlite3"), clock)
    nav, files = NAV, FILES
    ev.introduce(nav.skills)
    assert {s.state for s in ev.skill_states()} == {"introduced"}
    a = ev.start_attempt(None, nav, "guided")
    ev.record_assistance(a.id, 2, hint=True)
    ev.record_verification(a.id, nav, result(True))
    s = {x.skill: x for x in ev.skill_states()}["cli.navigate.cd"]
    assert s.state == "practiced_with_assistance" and s.assisted_passes == 1
    assert s.next_review_at == clock.t + 1 * DAY
    # Independent pass in the same exercise, then another context.
    a2 = ev.start_attempt(None, nav, "guided")
    assert a2.id != a.id
    ev.record_verification(a2.id, nav, result(True))
    s = {x.skill: x for x in ev.skill_states()}["cli.navigate.cd"]
    assert s.state == "demonstrated_independently"
    assert s.next_review_at == clock.t + 3 * DAY  # interval grows 1 -> 3 days
    assert ev.due_reviews() == []
    clock.t += 4 * DAY
    assert "cli.navigate.cd" in {x.skill for x in ev.due_reviews()}
    ev.record_review("cli.navigate.cd", passed=True, assistance=0, exercise_id="cli.files")
    s = {x.skill: x for x in ev.skill_states()}["cli.navigate.cd"]
    assert s.state == "demonstrated_new_context"
    assert s.next_review_at == clock.t + 7 * DAY
    ev.record_review("cli.navigate.cd", passed=False, assistance=0)
    s = {x.skill: x for x in ev.skill_states()}["cli.navigate.cd"]
    assert s.next_review_at == clock.t + 1 * DAY and s.failures == 1
    assert nav.id in ev.completed_exercises()
    assert files.id not in ev.completed_exercises()


def test_failed_check_does_not_finish_attempt(tmp_path: Path) -> None:
    ev = EvidenceStore(open_store(tmp_path / "db.sqlite3"))
    nav = NAV
    a = ev.start_attempt(None, nav, "assessment")
    ev.record_verification(a.id, nav, result(False))
    assert ev.current_attempt(nav.id) is not None
    assert ev.skill_states()[0].failures == 1


# ---------- export / delete / retention ----------
def test_export_has_no_secrets_and_delete_session(tmp_path: Path, memory_keyring: object) -> None:
    store = open_store(tmp_path / "db.sqlite3")
    sessions = Sessions(store)
    sid = sessions.create("mock", "m")
    sessions.add_message(sid, "learner", "hello")
    store.conn.execute(
        "INSERT INTO events(event_id, session_id, source_kind, source_id, seq, ingest_index, type, captured_at, received_at, payload) "
        "VALUES ('e1', ?, 'shell', 'z', 1, 1, 'shell.command_finished', 0, 0, '{}')",
        (sid,),
    )
    Notes(store).add("kept: I said this", session_id=sid)
    Notes(store).add("removed: model guess", provenance="model_inferred", session_id=sid)
    memory_keyring.set_password("cli-mentor", "openai", "sk-proj-SECRETSECRETSECRET123")  # type: ignore[attr-defined]
    data = export_json(store)
    assert "SECRETSECRET" not in data
    parsed = json.loads(data)
    assert parsed["sessions"][0]["id"] == sid and "messages" in parsed
    counts = delete_session(store, sid)
    assert counts["events"] == 1 and counts["messages"] == 1 and counts["memory_items"] == 1
    assert sessions.get(sid) is None
    assert [i.text for i in Notes(store).items()] == ["kept: I said this"]
    wal = Path(str(store.path) + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0


def test_retention_purge(tmp_path: Path) -> None:
    store = open_store(tmp_path / "db.sqlite3")
    sid = Sessions(store).create("mock", "m")
    store.conn.execute(
        "INSERT INTO events(event_id, session_id, source_kind, source_id, seq, ingest_index, type, captured_at, received_at, payload) "
        "VALUES ('old', ?, 'shell', 'z', 1, 1, 't', 0, 0, '{}'), ('new', ?, 'shell', 'z', 2, 2, 't', 0, 9e9, '{}')",
        (sid, sid),
    )
    out = purge_expired(store, Config())
    assert out["events"] == 1
    assert store.conn.execute("SELECT event_id FROM events").fetchall()[0][0] == "new"
