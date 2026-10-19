"""`mentor` command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__
from .apikeys import ENV_VARS, delete_api_key, get_api_key, set_api_key
from .config import Config, load_config
from .db.store import open_store
from .doctor import format_results, run_checks
from .logs import setup_logging
from .memory.notes import Notes
from .memory.privacy_ops import delete_session, export_json
from .memory.profile import Profile
from .memory.sessions import Sessions
from .paths import AppPaths

PROVIDERS = ("mock", "anthropic", "openai")


def _paths() -> AppPaths:
    paths = AppPaths.from_env()
    paths.ensure()
    return paths


def _config(paths: AppPaths) -> Config:
    config = load_config(paths.config_file)
    override = os.environ.get("CLI_MENTOR_PROVIDER")
    if override in PROVIDERS:
        config.tutor.provider = override  # type: ignore[assignment]
    return config


def _require_init(config: Config) -> bool:
    if not config.initialized:
        print("CLI Mentor is not set up yet. Run `mentor init` first (or `mentor demo` to try it).")
        return False
    return True


def _fmt(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


# ---------------------------------------------------------------- commands
def cmd_doctor(args: argparse.Namespace) -> int:
    results = run_checks(_paths())
    if args.json:
        print(json.dumps([r.__dict__ for r in results], indent=2))
    else:
        print(format_results(results))
    return 1 if any(r.status == "FAIL" for r in results) else 0


def cmd_init(args: argparse.Namespace) -> int:
    from .onboarding import run_init

    paths = _paths()
    config = load_config(paths.config_file)
    run_init(
        paths,
        config,
        assume_yes=args.yes,
        provider=args.provider,
        model=args.model,
        workspace=args.workspace,
        base_url=args.base_url,
    )
    return 0


def _preflight(paths: AppPaths) -> bool:
    failures = [r for r in run_checks(paths) if r.status == "FAIL"]
    if failures:
        print("Cannot start yet:\n")
        for r in failures:
            print(f"  - {r.name}: {r.detail}\n    fix: {r.fix}")
        return False
    return True


def cmd_start(args: argparse.Namespace) -> int:
    from .lifecycle import attach, cleanup_stale_runtime, create_workspace

    paths = _paths()
    if args.provider:
        os.environ["CLI_MENTOR_PROVIDER"] = args.provider
    config = _config(paths)
    if not _require_init(config) or not _preflight(paths):
        return 1
    store = open_store(paths.db_file)
    sessions = Sessions(store)
    settings = config.provider_settings()
    sid = sessions.create(config.tutor.provider, settings.model)
    cleanup_stale_runtime(paths, {s.id for s in sessions.list_sessions(500)})
    try:
        info = create_workspace(config, paths, sid)
    except Exception as exc:
        sessions.end(sid)
        print(f"Could not create the workspace: {exc}")
        return 1
    sessions.touch(sid, tmux_session=info.tmux_session)
    store.close()
    from .lifecycle import socket_name

    print(f"Session {sid} ready in tmux session '{info.tmux_session}' (private tmux server '{socket_name()}').")
    if os.environ.get("CLI_MENTOR_DEMO"):
        print("DEMO MODE: real tmux, Neovim and zsh; the tutor is the offline MOCK (not an AI).")
    if args.detach:
        print(f"Attach later with: mentor resume {sid}")
        return 0
    return attach(paths, sid, nested=args.nested)


def cmd_resume(args: argparse.Namespace) -> int:
    from .lifecycle import attach, create_workspace, make_tmux, repair_tutor, tmux_session_name

    paths = _paths()
    config = _config(paths)
    if not _require_init(config):
        return 1
    store = open_store(paths.db_file)
    sessions = Sessions(store)
    info = (
        sessions.find(args.session)
        if args.session
        else next((s for s in sessions.list_sessions(50) if s.status == "active"), sessions.latest())
    )
    if info is None:
        print("No session to resume. Start one with `mentor start`.")
        return 1
    tmux = make_tmux(paths)
    if tmux.has_session(tmux_session_name(info.id)):
        if repair_tutor(paths, info.id):
            print("The tutor pane had stopped; restarted it.")
    else:
        if not _preflight(paths):
            return 1
        sessions.reopen(info.id)
        ws = create_workspace(config, paths, info.id)
        sessions.touch(info.id, tmux_session=ws.tmux_session)
        print(f"Recreated the workspace for session {info.id}; your learning state is kept.")
    store.close()
    if args.detach:
        return 0
    return attach(paths, info.id, nested=args.nested)


def cmd_stop(args: argparse.Namespace) -> int:
    from .lifecycle import cleanup_stale_runtime, editor_busy, stop_workspace

    paths = _paths()
    store = open_store(paths.db_file)
    sessions = Sessions(store)
    info = (
        sessions.find(args.session)
        if args.session
        else next((s for s in sessions.list_sessions(50) if s.status == "active"), None)
    )
    if info is None:
        print("No active session found.")
        return 1
    busy = editor_busy(paths, info.id)
    if busy and not args.force:
        print("Not stopping, because " + " and ".join(busy) + ".")
        print("Save and quit Neovim (Esc, then :wq), or use `mentor stop --force` to close anyway.")
        return 1
    stopped = stop_workspace(paths, info.id)
    sessions.end(info.id)
    cleanup_stale_runtime(paths, {s.id for s in sessions.list_sessions(500) if s.status == "active"})
    print(f"Session {info.id} {'stopped' if stopped else 'was not running'}. Learning history is kept.")
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    from .lifecycle import make_tmux, tmux_session_name

    paths = _paths()
    store = open_store(paths.db_file)
    sessions = Sessions(store)
    live = set(make_tmux(paths).sessions())
    rows = sessions.list_sessions(args.limit)
    if not rows:
        print("No sessions yet.")
        return 0
    print(f"{'SESSION':28} {'CREATED':17} {'STATUS':7} {'TMUX':5} {'PROVIDER':10} EXERCISE")
    for s in rows:
        st = sessions.state(s.id)
        alive = "yes" if tmux_session_name(s.id) in live else "no"
        print(
            f"{s.id:28} {_fmt(s.created_at):17} {s.status:7} {alive:5} {s.provider or '-':10} "
            f"{st.active_exercise or '-'} ({st.exercise_stage or '-'})"
        )
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    store = open_store(_paths().db_file)
    prof = Profile(store)
    action = args.action
    if action == "confirm":
        n = prof.confirm_all_proposed() if args.key == "all" else int(prof.confirm(args.key))
        print(f"Confirmed {n}.")
    elif action == "reject":
        print("Rejected." if prof.reject(args.key) else "No such key.")
    elif action == "set":
        prof.set(args.key, " ".join(args.text))
        print("Saved.")
    elif action == "remove":
        print("Removed." if prof.remove(args.key) else "No such key.")
    else:
        for f in prof.facts(include_rejected=True):
            print(f"{f.key:28} [{f.status}, {f.provenance}] {f.value}")
    return 0


def cmd_memory(args: argparse.Namespace) -> int:
    store = open_store(_paths().db_file)
    notes = Notes(store)
    a = args.action
    if a == "search":
        items = notes.search(" ".join(args.rest))
    elif a == "add":
        print(f"Saved #{notes.add(' '.join(args.rest))}.")
        return 0
    elif a in ("confirm", "reject", "forget"):
        ok = {"confirm": notes.confirm, "reject": notes.reject, "forget": notes.forget}[a](int(args.rest[0]))
        print("Done." if ok else "No such note.")
        return 0
    else:
        items = notes.items()
    for i in items:
        print(f"#{i.id:<4} [{i.status}, {i.provenance}] {i.text}")
    if not items:
        print("No memory notes.")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    store = open_store(_paths().db_file)
    data = export_json(store, include_transcripts=not args.no_transcripts, include_events=args.include_events)
    if args.out:
        out = Path(args.out).expanduser()
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(data)
        print(f"Exported to {out} (no API keys; observed text was redacted before storage, best effort).")
    else:
        print(data)
    return 0


def cmd_delete_session(args: argparse.Namespace) -> int:
    from .lifecycle import stop_workspace

    paths = _paths()
    store = open_store(paths.db_file)
    sessions = Sessions(store)
    info = sessions.find(args.session)
    if info is None:
        print("No session matches that id.")
        return 1
    what = "observations, transcript, model suggestions, usage" + (
        ", and learning evidence" if args.with_evidence else ""
    )
    if not args.yes:
        answer = input(f"Delete {what} for session {info.id}? This cannot be undone. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing deleted.")
            return 1
    stop_workspace(paths, info.id)
    counts = delete_session(store, info.id, with_evidence=args.with_evidence)
    print("Deleted: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    print(
        "The database was compacted (WAL folded in, VACUUM). Copies may still exist in backups such as "
        "Time Machine; data already sent to a provider cannot be recalled."
    )
    return 0


def cmd_key(args: argparse.Namespace) -> int:
    if args.action == "set":
        import getpass

        key = getpass.getpass(f"Paste your {args.provider} API key (hidden): ").strip()
        if not key:
            print("Nothing saved.")
            return 1
        set_api_key(args.provider, key)
        print("Saved to the macOS Keychain (service 'cli-mentor').")
    elif args.action == "delete":
        print("Deleted from Keychain." if delete_api_key(args.provider) else "No Keychain entry found.")
    else:
        lookup = get_api_key(args.provider)
        print(
            f"{args.provider}: {'found in ' + lookup.source if lookup.key else 'not found'} "
            f"(env var {ENV_VARS[args.provider]} is checked first; the value is never printed)"
        )
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from .onboarding import run_init

    base = AppPaths.from_env()
    home = base.cache_dir / "demo"
    os.environ["CLI_MENTOR_HOME"] = str(home)
    os.environ["CLI_MENTOR_DEMO"] = "1"
    os.environ["CLI_MENTOR_PROVIDER"] = "mock"
    os.environ.setdefault("CLI_MENTOR_RUNTIME_DIR", str(base.runtime_dir / "demo"))
    paths = _paths()
    config = load_config(paths.config_file)
    if not config.initialized:
        run_init(
            paths, config, assume_yes=True, provider="mock", workspace=str(home / "workspace"), say=lambda _m: None
        )
    print(f"DEMO: real tmux, Neovim and zsh with the offline MOCK tutor (not an AI). Demo data: {home}")
    return cmd_start(argparse.Namespace(provider="mock", detach=args.detach, nested=args.nested))


def cmd_tutor(args: argparse.Namespace) -> int:
    from .providers import build_provider
    from .providers.base import ProviderError
    from .tmuxutil import Tmux
    from .tutor.engine import Engine
    from .ui.app import MentorApp

    paths = _paths()
    setup_logging(paths.log_file)
    config = _config(paths)
    store = open_store(paths.db_file)
    sessions = Sessions(store)
    info = sessions.get(args.session)
    if info is None:
        print(f"Unknown session {args.session}")
        return 1
    notes: list[tuple[str, str]] = []
    if os.environ.get("CLI_MENTOR_DEMO"):
        notes.append(("info", "**DEMO MODE.** Real tmux, Neovim and zsh. The tutor is the offline MOCK, not an AI."))
    try:
        provider = build_provider(config)
    except ProviderError as exc:
        provider = build_provider(config, "mock")
        notes.append(("error", exc.user_message() + " Using the offline MOCK tutor until this is fixed."))
    tmux = Tmux(
        os.environ.get("CLI_MENTOR_TMUX_SOCKET", "cli-mentor"),
        config_file=os.environ.get("CLI_MENTOR_TMUX_CONF") or None,
    )
    tmux_session = info.tmux_session
    tutor_pane = os.environ.get("TMUX_PANE")
    in_tmux = bool(tutor_pane and tmux_session and tmux.has_session(tmux_session))

    def factory(ui: MentorApp) -> Engine:
        return Engine(
            config=config,
            paths=paths,
            store=store,
            session_id=info.id,
            provider=provider,
            ui=ui,
            tmux=tmux if in_tmux else None,
            tmux_session=tmux_session if in_tmux else None,
            tutor_pane=tutor_pane,
        )

    async def detach() -> None:
        import asyncio

        if in_tmux and tmux_session:
            await asyncio.to_thread(tmux.run, "detach-client", "-s", f"={tmux_session}", check=False)

    app = MentorApp(factory, on_detach=detach, startup_notes=notes)
    result = app.run()
    if result == "end":
        import shutil

        shutil.rmtree(paths.runtime_dir / info.id[:12], ignore_errors=True)  # token, zsh wrapper files
        if in_tmux and tmux_session:
            tmux.kill_session(tmux_session)  # closes this pane too; nothing runs after this
    return 0


# ---------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mentor", description="CLI Mentor: a tutor beside your real terminal and Neovim.")
    p.add_argument("--version", action="version", version=f"cli-mentor {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="check dependencies and settings (installs nothing)")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_doctor)

    i = sub.add_parser("init", help="set up: profile, practice folder, model provider")
    i.add_argument("--yes", action="store_true", help="accept defaults without asking")
    i.add_argument("--provider", choices=PROVIDERS)
    i.add_argument("--model")
    i.add_argument("--workspace")
    i.add_argument("--base-url", help="OpenAI-compatible endpoint (unverified unless tested)")
    i.set_defaults(func=cmd_init)

    for name, func, text in (
        ("start", cmd_start, "start a new learning session"),
        ("resume", cmd_resume, "re-attach to (or rebuild) a session"),
    ):
        s = sub.add_parser(name, help=text)
        if name == "start":
            s.add_argument("--provider", choices=PROVIDERS, help="override the configured provider for this session")
        else:
            s.add_argument("session", nargs="?", help="session id or prefix (default: latest)")
        s.add_argument("--detach", action="store_true", help="create it but do not attach")
        s.add_argument("--nested", action="store_true", help="attach even inside tmux")
        s.set_defaults(func=func)

    st = sub.add_parser("stop", help="stop a session's panes (checks for unsaved work)")
    st.add_argument("session", nargs="?")
    st.add_argument("--force", action="store_true")
    st.set_defaults(func=cmd_stop)

    ss = sub.add_parser("sessions", help="list sessions")
    ss.add_argument("--limit", type=int, default=20)
    ss.set_defaults(func=cmd_sessions)

    pr = sub.add_parser("profile", help="show or edit your profile")
    pr.add_argument("action", nargs="?", choices=["show", "confirm", "reject", "set", "remove"], default="show")
    pr.add_argument("key", nargs="?")
    pr.add_argument("text", nargs="*")
    pr.set_defaults(func=cmd_profile)

    m = sub.add_parser("memory", help="list, search, or edit remembered notes")
    m.add_argument(
        "action", nargs="?", choices=["list", "search", "add", "confirm", "reject", "forget"], default="list"
    )
    m.add_argument("rest", nargs="*")
    m.set_defaults(func=cmd_memory)

    e = sub.add_parser("export", help="export your data as JSON (no secrets)")
    e.add_argument("--out")
    e.add_argument("--include-events", action="store_true")
    e.add_argument("--no-transcripts", action="store_true")
    e.set_defaults(func=cmd_export)

    ds = sub.add_parser("delete-session", help="delete one session's stored data")
    ds.add_argument("session")
    ds.add_argument("--with-evidence", action="store_true")
    ds.add_argument("--yes", action="store_true")
    ds.set_defaults(func=cmd_delete_session)

    k = sub.add_parser("key", help="manage API keys in the macOS Keychain")
    k.add_argument("action", choices=["set", "delete", "status"])
    k.add_argument("provider", choices=["anthropic", "openai"])
    k.set_defaults(func=cmd_key)

    dm = sub.add_parser("demo", help="offline demo: real tools, mock tutor, separate data")
    dm.add_argument("--detach", action="store_true")
    dm.add_argument("--nested", action="store_true")
    dm.set_defaults(func=cmd_demo)

    t = sub.add_parser("tutor", help=argparse.SUPPRESS)
    t.add_argument("--session", required=True)
    t.set_defaults(func=cmd_tutor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
