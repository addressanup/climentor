"""Slash commands typed in the tutor pane. The help text is the reference."""

from __future__ import annotations

import shlex
import time
from enum import Enum

from ..curriculum.loader import TRACK_TITLES, find_lesson, lessons_by_track
from ..memory.evidence import ASSISTANCE_LABELS
from ..memory.privacy_ops import delete_observations
from ..memory.profile import INTERVENTION_CHOICES, INTERVENTION_KEY
from ..permissions import DESCRIPTIONS, Capability
from ..providers.base import ProviderError
from .engine import Engine
from .policy import Trigger

HELP = """\
**Commands** (type them in the tutor pane; anything else is a message to the tutor)

| Command | What it does |
|---|---|
| `/help` | Show this list |
| `/goal [text]` | Show or set your goal for this session |
| `/lesson` | Start or continue the next exercise |
| `/lesson list` | List all exercises and your status |
| `/lesson start <id>` | Start one exercise, e.g. `/lesson start cli.pipelines` |
| `/lesson skip` | Skip the prediction question and show the task |
| `/lesson reset` | Recreate the exercise's starting files (asks first) |
| `/lesson cleanup` | Remove unchanged files the exercise created (asks first) |
| `/hint` | Next, slightly stronger hint (recorded as help) |
| `/solution` | Show the full solution (asks; recorded as full help) |
| `/explain [term]` | Explain a word or symbol from the current lesson |
| `/check [answer]` | Run the trusted checker (some exercises need an answer) |
| `/review` | Skills due for review, and a review question |
| `/progress` | Your skill states and the evidence behind them |
| `/profile` | Your profile, with where each fact came from |
| `/profile confirm <key>\\|all` | Confirm proposed profile facts |
| `/profile reject <key>` | Reject a proposed fact |
| `/profile set <key> <text>` | Add or change a fact in your own words |
| `/profile remove <key>` | Delete a fact |
| `/profile style quiet\\|balanced\\|proactive` | How often the tutor may speak unasked |
| `/memory` | Remembered notes, with provenance |
| `/memory search <words>` | Search memory |
| `/memory add <text>` | Remember something you state |
| `/memory confirm <id>` / `reject <id>` | Accept or refuse a tutor suggestion |
| `/memory correct <id> <text>` | Replace a note with your correction |
| `/forget <id>` | Delete one memory note |
| `/forget observations` | Delete stored observations for this session |
| `/mode [guided\\|practice\\|project\\|assessment\\|review\\|quiet]` | Show or change teaching mode |
| `/observe` | Observation status and scope |
| `/observe pause` / `resume` | Stop or restart observation |
| `/permissions` | Active permissions |
| `/permissions revoke <capability\\|id>` | Revoke a permission |
| `/context` | Exactly what would be sent to the model (redacted) |
| `/model` | Provider, model, and token usage |
| `/model list` | Models the provider offers |
| `/cancel` | Stop the tutor's current reply (or press Esc) |
| `/quit` | Leave: detach, end the session, or cancel |

Keys: **Enter** sends · **Ctrl+J** or **Alt+Enter** adds a new line · **Esc** cancels a reply ·
**Ctrl+Q** quits · in tmux, **Ctrl+b** then an arrow key moves between panes, **Ctrl+b z** zooms a pane.
"""

STATE_LABELS = {
    "introduced": "introduced",
    "practiced_with_assistance": "practised with help",
    "demonstrated_independently": "done on your own",
    "demonstrated_new_context": "done on your own in a new context",
}


class Result(Enum):
    HANDLED = "handled"
    QUIT = "quit"


def _fmt_time(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


async def dispatch(engine: Engine, line: str) -> Result:
    try:
        parts = shlex.split(line[1:])
    except ValueError:
        parts = line[1:].split()
    if not parts:
        engine.ui.note(HELP)
        return Result.HANDLED
    cmd, args = parts[0].lower(), parts[1:]
    rest = line[1:].strip()[len(parts[0]) :].strip()
    ui = engine.ui

    if cmd == "help":
        ui.note(HELP)
    elif cmd == "quit":
        return Result.QUIT
    elif cmd == "cancel":
        if not await engine.cancel_turn("cancelled", "stopped by you"):
            ui.note("Nothing to cancel.")
    elif cmd == "goal":
        if rest:
            engine.session.goal = rest[:200]
            engine.sessions.save_state(engine.session)
            engine.push_status()
            ui.note(f"Goal set: {engine.session.goal}", "success")
        else:
            ui.note(f"Goal: {engine.session.goal or 'not set. Use /goal <text>.'}")
    elif cmd == "lesson":
        await _lesson(engine, args)
    elif cmd == "hint":
        await engine.hint()
    elif cmd == "solution":
        await engine.solution()
    elif cmd == "explain":
        await engine.run_turn(Trigger.EXPLAIN, learner_text=rest)
    elif cmd == "check":
        await engine.check(answer=rest or None)
    elif cmd == "review":
        await _review(engine)
    elif cmd == "progress":
        _progress(engine)
    elif cmd == "profile":
        _profile(engine, args, rest)
    elif cmd == "memory":
        _memory(engine, args, rest)
    elif cmd == "forget":
        _forget(engine, args)
    elif cmd == "mode":
        _mode(engine, args)
    elif cmd == "observe":
        await _observe(engine, args)
    elif cmd == "permissions":
        _permissions(engine, args)
    elif cmd == "context":
        _context(engine)
    elif cmd == "model":
        await _model(engine, args)
    else:
        ui.note(f"Unknown command `/{cmd}`. Type `/help`.", "warn")
    return Result.HANDLED


async def _lesson(engine: Engine, args: list[str]) -> None:
    sub = args[0].lower() if args else ""
    if sub == "list":
        done = engine.evidence.completed_exercises()
        current = engine.session.active_exercise
        lines = []
        for track, items in lessons_by_track().items():
            lines.append(f"**{TRACK_TITLES.get(track, track)}**")
            for les in items:
                mark = "[done]" if les.id in done else ("[now]" if les.id == current else "[ ]")
                lines.append(f"- {mark} `{les.id}` — {les.title} (~{les.estimated_minutes} min)")
        engine.ui.note("\n".join(lines))
    elif sub == "start" and len(args) > 1:
        await engine.start_lesson(args[1])
    elif sub == "skip":
        await engine.skip_prediction()
    elif sub == "reset":
        if engine.session.active_exercise:
            await engine.start_lesson(engine.session.active_exercise, reset=True)
        else:
            engine.ui.note("No exercise is active.")
    elif sub == "cleanup":
        await engine.cleanup_lesson()
    elif sub and find_lesson(sub):
        await engine.start_lesson(sub)
    elif sub:
        engine.ui.note(
            "Usage: `/lesson`, `/lesson list`, `/lesson start <id>`, `/lesson skip`, `/lesson reset`, `/lesson cleanup`.",
            "warn",
        )
    else:
        lesson = engine.lesson()
        if lesson is not None and engine.session.exercise_stage != "verified":
            engine.ui.note(f"Continuing “{lesson.title}” (stage: {engine.session.exercise_stage}).")
            if engine.session.exercise_stage == "explain":
                await engine.run_turn(Trigger.LESSON_INTRO)
            else:
                engine.ui.note(f"**Your task:**\n\n{lesson.instructions.strip()}")
        else:
            await engine.start_lesson(None)


async def _review(engine: Engine) -> None:
    due = engine.evidence.due_reviews()
    if not due:
        states = engine.evidence.skill_states()
        upcoming = sorted((s for s in states if s.next_review_at), key=lambda s: s.next_review_at or 0)
        msg = "Nothing is due for review right now."
        if upcoming:
            msg += f" Next: {upcoming[0].skill} on {_fmt_time(upcoming[0].next_review_at)}."
        engine.ui.note(msg)
        return
    skills = [s.skill for s in due[:5]]
    engine.ui.note(
        "Due for review: " + ", ".join(skills) + ".\nYou can also redo an exercise with `/lesson start <id>`."
    )
    await engine.run_turn(Trigger.REVIEW, learner_text="Review these skills: " + ", ".join(skills))


def _progress(engine: Engine) -> None:
    states = engine.evidence.skill_states()
    if not states:
        engine.ui.note("No skill evidence yet. Finish an exercise with `/check` to start your record.")
        return
    now = time.time()
    lines = [
        "**Your skills** (from trusted checks; no percentages, because we have no model that justifies one)",
        "",
    ]
    for s in states:
        due = (
            " · **due for review**"
            if s.due(now)
            else (f" · review {_fmt_time(s.next_review_at)}" if s.next_review_at else "")
        )
        lines.append(
            f"- `{s.skill}`: {STATE_LABELS[s.state]} — {s.independent_passes} on your own, "
            f"{s.assisted_passes} with help, {s.failures} not yet{due}"
        )
    attempts = engine.store.conn.execute(
        "SELECT exercise_id, status, max_assistance, hints_used, solution_shown FROM attempts ORDER BY started_at DESC LIMIT 8"
    ).fetchall()
    if attempts:
        lines += ["", "**Recent attempts**"]
        for a in attempts:
            lines.append(
                f"- {a['exercise_id']}: {a['status']}, most help: {ASSISTANCE_LABELS[a['max_assistance']]}, hints: {a['hints_used']}"
            )
    engine.ui.note("\n".join(lines))


def _profile(engine: Engine, args: list[str], rest: str) -> None:
    p = engine.profile
    sub = args[0].lower() if args else ""
    if sub == "confirm" and len(args) > 1:
        n = p.confirm_all_proposed() if args[1] == "all" else int(p.confirm(args[1]))
        engine.ui.note(f"Confirmed {n} fact(s).", "success")
    elif sub == "reject" and len(args) > 1:
        engine.ui.note("Rejected." if p.reject(args[1]) else "No such key.")
    elif sub == "set" and len(args) > 2:
        p.set(args[1], rest.split(None, 2)[2])
        engine.ui.note(f"Saved `{args[1]}` (you stated it).", "success")
    elif sub == "remove" and len(args) > 1:
        engine.ui.note("Removed." if p.remove(args[1]) else "No such key.")
    elif sub == "style" and len(args) > 1 and args[1] in INTERVENTION_CHOICES:
        p.set(INTERVENTION_KEY, args[1])
        engine.ui.note(f"Intervention style: {args[1]}.", "success")
    elif sub:
        engine.ui.note(
            "Usage: `/profile`, `/profile confirm <key>|all`, `/profile reject <key>`, `/profile set <key> <text>`, "
            "`/profile remove <key>`, `/profile style quiet|balanced|proactive`.",
            "warn",
        )
    else:
        facts = p.facts(include_rejected=True)
        lines = ["**Your profile** (only *confirmed* facts are given to the model)", ""]
        for f in facts:
            lines.append(f"- `{f.key}` [{f.status}, {f.provenance.replace('_', ' ')}]: {f.value}")
        lines.append("")
        lines.append(f"Intervention style: {p.intervention_style()}")
        engine.ui.note(
            "\n".join(lines) if facts else "Profile is empty. Run `mentor init` or `/profile set <key> <text>`."
        )


def _memory(engine: Engine, args: list[str], rest: str) -> None:
    n = engine.notes
    sub = args[0].lower() if args else ""
    try:
        if sub == "search" and len(args) > 1:
            items = n.search(rest.split(None, 1)[1])
        elif sub == "add" and len(args) > 1:
            item_id = n.add(rest.split(None, 1)[1], provenance="user_stated", session_id=engine.session_id)
            engine.ui.note(f"Remembered as #{item_id} (you stated it).", "success")
            return
        elif sub == "confirm" and len(args) > 1:
            engine.ui.note("Confirmed." if n.confirm(int(args[1])) else "No such note.")
            return
        elif sub == "reject" and len(args) > 1:
            engine.ui.note("Rejected." if n.reject(int(args[1])) else "No such note.")
            return
        elif sub == "correct" and len(args) > 2:
            new = n.correct(int(args[1]), rest.split(None, 2)[2])
            engine.ui.note(f"Replaced with #{new}." if new else "No such note.")
            return
        elif sub:
            engine.ui.note(
                "Usage: `/memory`, `/memory search <words>`, `/memory add <text>`, `/memory confirm <id>`, "
                "`/memory reject <id>`, `/memory correct <id> <text>`.",
                "warn",
            )
            return
        else:
            items = n.items()
    except ValueError:
        engine.ui.note("Note ids are numbers, e.g. `/memory confirm 3`.", "warn")
        return
    if not items:
        engine.ui.note("No memory notes.")
        return
    engine.ui.note("\n".join(f"- #{i.id} [{i.status}, {i.provenance.replace('_', ' ')}] {i.text}" for i in items))


def _forget(engine: Engine, args: list[str]) -> None:
    if not args:
        engine.ui.note("Usage: `/forget <id>` or `/forget observations`.", "warn")
    elif args[0] == "observations":
        count = delete_observations(engine.store, engine.session_id)
        engine.ui.note(
            f"Deleted {count} stored observation(s) for this session and compacted the database. "
            "Data already sent to a provider cannot be recalled.",
            "success",
        )
    else:
        try:
            ok = engine.notes.forget(int(args[0]))
        except ValueError:
            ok = False
        engine.ui.note("Deleted." if ok else "No such note.")


def _mode(engine: Engine, args: list[str]) -> None:
    modes = ("guided", "practice", "project", "assessment", "review", "quiet")
    if not args:
        engine.ui.note(f"Mode: **{engine.session.mode}**. Options: {', '.join(modes)}.")
        return
    if args[0] not in modes:
        engine.ui.note(f"Unknown mode. Options: {', '.join(modes)}.", "warn")
        return
    engine.session.mode = args[0]
    engine.sessions.save_state(engine.session)
    engine.push_status()
    engine.ui.note(f"Mode set to **{args[0]}**.", "success")


async def _observe(engine: Engine, args: list[str]) -> None:
    sub = args[0].lower() if args else ""
    if sub == "pause":
        await engine.pause_observation()
    elif sub == "resume":
        await engine.resume_observation()
    else:
        info = engine.status_info()
        roots = "\n".join(f"- {r}" for r in engine.scope.roots)
        detail = "\n".join(
            f"- {s.kind.value} `{s.id}`: {s.status.value}{(' — ' + s.detail) if s.detail else ''}"
            for s in engine.state.sources.values()
        )
        engine.ui.note(
            f"Observation: **{info.observation}** · sync: **{info.sync}**\n\nApproved folders:\n{roots}\n\n"
            f"Sources:\n{detail or '- none yet'}\n\nDropped under load: {engine.dropped_events} · "
            f"discarded while paused: {engine.dropped_while_paused}\n\nUse `/observe pause` or `/observe resume`."
        )


def _permissions(engine: Engine, args: list[str]) -> None:
    pm = engine.permissions
    if args and args[0] == "revoke" and len(args) > 1:
        target = args[1]
        if target.isdigit():
            n = pm.revoke(grant_id=int(target))
        else:
            try:
                n = pm.revoke(Capability(target))
            except ValueError:
                engine.ui.note(
                    f"Unknown capability. Options: {', '.join(c.value for c in Capability)}.",
                    "warn",
                )
                return
        engine.ui.note(f"Revoked {n} grant(s).", "success")
        engine.push_status()
        return
    grants = pm.active_grants()
    lines = ["**Active permissions**", ""]
    for g in grants:
        lines.append(
            f"- #{g.id} `{g.capability.value}` ({g.scope.value}){' on ' + g.resource if g.resource else ''} — {DESCRIPTIONS[g.capability]}"
        )
    if not grants:
        lines.append("- none")
    lines += [
        "",
        "Never available in this version: run_shell, install_software, mutate_database.",
        "Revoke with `/permissions revoke <capability|id>`.",
    ]
    engine.ui.note("\n".join(lines))


def _context(engine: Engine) -> None:
    from .context import TurnInputs

    inputs = TurnInputs(
        trigger=Trigger.LEARNER_MESSAGE,
        mode=engine.session.mode,
        session_id=engine.session_id,
        lesson=engine.lesson(),
        stage=engine.session.exercise_stage,
        observation_allowed=not engine.paused and engine.observing,
        exercise_dir=engine.exercise_dir(),
        goal=engine.session.goal,
    )
    settings = engine.config.provider_settings(engine.provider.name)  # type: ignore[arg-type]
    ctx = engine.assembler.build(
        inputs,
        max_context_tokens=settings.max_context_tokens,
        max_output_tokens=settings.max_output_tokens,
    )
    engine.ui.note("```text\n" + ctx.render_for_display().replace("```", "ʼʼʼ") + "\n```")


async def _model(engine: Engine, args: list[str]) -> None:
    p = engine.provider
    if args and args[0] == "list":
        try:
            models = await p.list_models()
        except ProviderError as exc:
            engine.ui.note(exc.user_message(), "error")
            return
        engine.ui.note(
            "Models offered:\n"
            + "\n".join(f"- `{m}`" for m in models[:60])
            + "\n\nChange with `mentor init` (it stores the choice in config.toml)."
        )
        return
    u = engine.sessions.usage_totals(engine.session_id)
    settings = engine.config.provider_settings(p.name)  # type: ignore[arg-type]
    cost = ""
    if settings.input_price_per_mtok is not None and settings.output_price_per_mtok is not None:
        est = u["input"] / 1e6 * settings.input_price_per_mtok + u["output"] / 1e6 * settings.output_price_per_mtok
        cost = f"\nEstimated cost (from YOUR configured prices): ${est:.4f}"
    kind = "offline mock (no network)" if not p.makes_network_calls else "real provider (network)"
    engine.ui.note(
        f"Provider: **{p.name}** ({kind}) · model: `{p.model}`\n"
        f"This session: {u['calls']} call(s), {u['input']} input + {u['output']} output tokens"
        f"{' (some estimated)' if u['estimated_calls'] else ' (reported by the provider)'}{cost}"
    )
