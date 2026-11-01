"""Slash commands typed in the tutor pane. The help text is the reference."""

from __future__ import annotations

import shlex
import time
from enum import Enum

from ..memory.privacy_ops import delete_observations
from ..memory.profile import INTERVENTION_CHOICES, INTERVENTION_KEY
from ..permissions import DESCRIPTIONS, Capability
from ..providers.base import ProviderError
from .engine import Engine
from .policy import Trigger

HELP = """\
**You never need these.** Just talk to me: say “done” when you finish, “hint” if you are stuck, “next” to
move on, “skip” to skip, “show the plan”, or “new subject: <topic>”. The commands below are optional shortcuts.

| Command | What it does |
|---|---|
| `/help` | Show this list |
| `/tour` | A short walkthrough of the screen and how we work |
| `/goal [text]` | Show or set your goal for this session |
| `/learn <topic>` | Start a new learning plan, e.g. `/learn C++ from the basics` |
| `/plan` | Show your plan and where you are |
| `/plan accept` | Accept the proposed plan (same as replying “yes”) |
| `/plan skip` | Skip the current step |
| `/plan list` / `switch <id>` | See all your plans / switch to another one |
| `/lesson` | Start or continue the current step |
| `/lesson skip` | Skip the prediction question and show the task |
| `/lesson reset` | Recreate the step's starting files (asks first) |
| `/lesson cleanup` | Remove unchanged files the step created (asks first) |
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
    elif cmd == "tour":
        engine.show_tour()
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
    elif cmd == "plan":
        await _plan(engine, args, rest)
    elif cmd == "learn":
        await engine.new_goal(rest)
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
    if sub == "skip":
        await engine.skip_prediction()
    elif sub == "reset":
        await engine.start_step(reset=True)
    elif sub == "cleanup":
        await engine.cleanup_lesson()
    elif sub:
        engine.ui.note("Usage: `/lesson`, `/lesson skip`, `/lesson reset`, `/lesson cleanup`.", "warn")
    else:
        await engine.start_step()


async def _plan(engine: Engine, args: list[str], rest: str) -> None:
    sub = args[0].lower() if args else ""
    if sub == "accept":
        await engine.accept_plan()
    elif sub == "skip":
        await engine.skip_step()
    elif sub == "list":
        plans = engine.plans.list_plans()
        if not plans:
            engine.ui.note("No plans yet. Tell me what you want to learn, or type `/learn <topic>`.")
            return
        lines = []
        for pl in plans:
            done, total = engine.plans.progress(pl.id)
            lines.append(f"- `{pl.id[:8]}` **{pl.title}** — {pl.status}, {done}/{total} steps")
        engine.ui.note("\n".join(lines) + "\n\nSwitch with `/plan switch <id>`.")
    elif sub == "switch" and len(args) > 1:
        await engine.switch_plan(args[1])
    elif sub:
        engine.ui.note("Usage: `/plan`, `/plan accept`, `/plan skip`, `/plan list`, `/plan switch <id>`.", "warn")
    else:
        engine.ui.note(engine.plan_markdown())


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
        engine.ui.note("We have not built up a learning record yet. Your work and replies will build it as we go.")
        return
    now = time.time()
    lines = ["Here is what your work and explanations suggest so far."]
    descriptions = {
        "introduced": "We have introduced",
        "practiced_with_assistance": "You have practised with help",
        "demonstrated_independently": "You have used on your own",
        "demonstrated_new_context": "You have applied in a new setting",
    }
    for state, description in descriptions.items():
        names = [s.skill.replace("_", " ").replace(".", " ") for s in states if s.state == state]
        if names:
            lines.append(description + ": " + ", ".join(names) + ".")
    if any(s.due(now) for s in states):
        lines.append("Some of these ideas are ready to revisit. Ask me for a review whenever you like.")
    engine.ui.note("\n\n".join(lines))


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
    # The same inputs and limits as a real reply to a message, so this shows what is really sent.
    inputs = engine.turn_inputs(Trigger.LEARNER_MESSAGE)
    max_context, out_tokens = engine.token_budget()
    ctx = engine.assembler.build(inputs, max_context_tokens=max_context, max_output_tokens=out_tokens)
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
    plan = ""
    if p.name == "chatgpt":
        from ..chatgpt_auth import USAGE_URL

        plan = f"\nUsing your ChatGPT plan. Manage usage: {USAGE_URL}"
    engine.ui.note(
        f"Provider: **{p.name}** ({kind}) · model: `{p.model}`\n"
        f"This session: {u['calls']} call(s), {u['input']} input + {u['output']} output tokens"
        f"{' (some estimated)' if u['estimated_calls'] else ' (reported by the provider)'}{cost}{plan}"
    )
