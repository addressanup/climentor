"""The tutor engine: one orchestrator, deterministic subsystems around it.

Flow:
  sources (IPC: zsh/Neovim, tmux control mode)
    -> validation + scope checks -> EventEnvelope -> bounded queue
    -> Sequencer -> WorkspaceState (revisions) -> persistence (redacted)
    -> hooks: intervention policy, auto-checks, staleness of in-flight advice
  learner input -> commands / lesson flow / turns
  turns -> ContextAssembler -> Provider stream -> UI, with staleness checks
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..config import Config
from ..curriculum.loader import find_lesson, load_lessons, next_lesson
from ..curriculum.model import Lesson
from ..curriculum.runner import HostRunner
from ..curriculum.setup import apply_setup, cleanup, plan_setup
from ..curriculum.verify import VerificationResult, VerifyContext, get_verifier
from ..db.store import Store
from ..events.schema import (
    ALLOWED_CLIENT_TYPES,
    PAYLOAD_ADAPTER,
    EventEnvelope,
    NvimBufferSnapshot,
    NvimBufferWritten,
    NvimFocus,
    Payload,
    Provenance,
    SourceKind,
    SourceRef,
    TmuxOutput,
)
from ..events.sequencing import Sequencer
from ..memory.evidence import EvidenceStore
from ..memory.notes import Notes
from ..memory.privacy_ops import purge_expired
from ..memory.profile import Profile
from ..memory.sessions import Sessions
from ..observe.ipc import ClientIdentity, IpcServer, new_token
from ..observe.privacy import redact_text
from ..observe.terminal import TerminalObserver
from ..observe.workspace import WorkspaceScope
from ..paths import AppPaths, write_private_file
from ..permissions import Capability, PermissionManager, Scope
from ..providers.base import (
    Done,
    Provider,
    ProviderError,
    ProviderRequest,
    TextDelta,
    ToolCall,
    TurnMeta,
    Usage,
)
from ..providers.mock import LABEL as MOCK_LABEL
from ..providers.mock import compose
from ..state.projection import WorkspaceState
from ..tmuxutil import Tmux
from . import actions
from .context import AssembledContext, ContextAssembler, TurnInputs
from .policy import SOLICITED, InterventionPolicy, PolicySettings, Trigger
from .staleness import ContextRevision, assess
from .ui_protocol import StatusInfo, UiSink

log = logging.getLogger(__name__)

NOT_PERSISTED = frozenset({"tmux.output", "nvim.cursor"})
APPROVE_CHOICES = [("y", "Yes, this time"), ("s", "Yes, for this session"), ("n", "No")]
SCOPE_FOR = {"y": Scope.ONCE, "s": Scope.SESSION, "p": Scope.PERSISTENT}


def _redact_obj(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redact_obj(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact_obj(v) for k, v in value.items()}
    return value


@dataclass
class InFlight:
    turn_id: int
    trigger: Trigger
    revision: ContextRevision
    task: asyncio.Task[None]
    cancel_status: str = "cancelled"
    cancel_note: str = ""

    @property
    def solicited(self) -> bool:
        return self.trigger in SOLICITED


class Engine:
    def __init__(
        self,
        *,
        config: Config,
        paths: AppPaths,
        store: Store,
        session_id: str,
        provider: Provider,
        ui: UiSink,
        tmux: Tmux | None = None,
        tmux_session: str | None = None,
        tutor_pane: str | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.paths = paths
        self.store = store
        self.session_id = session_id
        self.provider = provider
        self.ui = ui
        self.tmux = tmux
        self.tmux_session = tmux_session
        self.tutor_pane = tutor_pane
        self.clock = clock
        self.monotonic = monotonic
        self.workspace_root = Path(config.workspace_root).expanduser().resolve()
        self.scope = WorkspaceScope(config.roots(), config.observation.max_file_bytes)
        self.state = WorkspaceState(clock)
        self.sequencer = Sequencer()
        self.permissions = PermissionManager(store, session_id, clock)
        self.sessions = Sessions(store)
        self.profile = Profile(store)
        self.notes = Notes(store)
        self.evidence = EvidenceStore(store, clock)
        self.assembler = ContextAssembler(
            self.state, self.scope, self.profile, self.notes, self.evidence, self.sessions, clock
        )
        t = config.tutor
        self.policy = InterventionPolicy(
            PolicySettings(t.unsolicited_cooldown_seconds, t.debounce_seconds, t.repeated_failure_threshold),
            monotonic,
        )
        self.session = self.sessions.state(session_id)
        if not self.session.mode or self.session.mode not in (
            "guided",
            "practice",
            "project",
            "assessment",
            "review",
            "quiet",
        ):
            self.session.mode = t.mode
        self.queue: asyncio.Queue[EventEnvelope] = asyncio.Queue(maxsize=config.observation.max_queue_events)
        self.paused = False
        self.observing = False
        self.ipc: IpcServer | None = None
        self.terminal: TerminalObserver | None = None
        self.inflight: InFlight | None = None
        self.turn_counter = 0
        self.ingest_index = 0
        self.last_activity = monotonic()
        self.dropped_events = 0
        self.dropped_while_paused = 0
        self.telemetry_enabled: set[str] = set()
        self._rejected_buffers: set[tuple[str, int]] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._processor: asyncio.Task[None] | None = None
        self._deferred: asyncio.TimerHandle | None = None
        self._status_text = ""
        self._last_tmux_status = 0.0
        self.socket_path: Path | None = None
        self.token_file: Path | None = None

    # ================================================================ lifecycle
    async def start(self, *, observe: bool = True, greet: bool = True) -> None:
        purge_expired(self.store, self.config)
        self._processor = asyncio.create_task(self._process_loop(), name="mentor-processor")
        if observe:
            await self._start_ipc()
            if await self._ensure_observation_consent():
                await self._start_terminal("baseline")
                self.observing = True
            else:
                self.ui.note(
                    "Observation is off. The tutor cannot see your panes. Use `/observe resume` to turn it on.",
                    "warn",
                )
        self.push_status()
        if greet:
            await self.greet()

    async def stop(self) -> None:
        await self.cancel_turn("cancelled", "session ending")
        if self._deferred is not None:
            self._deferred.cancel()
        if self.terminal is not None:
            await self.terminal.stop()
            self.terminal = None
        if self.ipc is not None:
            await self.ipc.stop()
            self.ipc = None
        for task in list(self._tasks):
            task.cancel()
        if self._processor is not None:
            self._processor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._processor
        self.sessions.save_state(self.session)
        with contextlib.suppress(Exception):
            await self.provider.aclose()

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("background task failed", exc_info=task.exception())
            self.ui.note(f"Internal error: {type(task.exception()).__name__}. See the log file.", "error")

    async def _start_ipc(self) -> None:
        run_dir = self.paths.session_runtime_dir(self.session_id)
        self.socket_path = self.paths.socket_path(self.session_id)
        self.token_file = run_dir / "token"
        token = self.token_file.read_text().strip() if self.token_file.exists() else ""
        if len(token) < 64:
            token = new_token()
            write_private_file(self.token_file, token)
        self.ipc = IpcServer(
            self.socket_path,
            token,
            self._on_ipc_message,
            on_connect=self._on_ipc_connect,
            hello_extra=self._nvim_hello,
        )
        await self.ipc.start()

    async def _start_terminal(self, reason: str) -> None:
        if self.tmux is None or self.tmux_session is None:
            return
        self.terminal = TerminalObserver(self.tmux, self.tmux_session, self._on_terminal, tutor_pane=self.tutor_pane)
        try:
            await self.terminal.start(reason)
        except Exception as exc:
            self.ui.note(f"Could not attach to the tmux session for observation: {exc}", "error")
            self.terminal = None

    async def _ensure_observation_consent(self) -> bool:
        root = str(self.workspace_root)
        if self.permissions.check(Capability.OBSERVE_TERMINAL).allowed:
            return True
        answer = await self.ui.ask(
            "Allow observation?",
            "The tutor would see:\n"
            "- output of the shell pane in THIS session (not your other terminals)\n"
            "- commands you run there (not ones starting with a space)\n"
            f"- Neovim buffers inside {root}, including unsaved edits\n"
            "Secrets are filtered on a best-effort basis. You can pause any time with /observe pause.",
            [("s", "Yes, for this session"), ("p", "Yes, remember"), ("n", "No")],
        )
        if answer not in ("s", "p"):
            return False
        scope = SCOPE_FOR[answer]
        self.permissions.grant(Capability.OBSERVE_TERMINAL, scope)
        self.permissions.grant(Capability.OBSERVE_EDITOR, scope, root)
        self.permissions.grant(Capability.READ_FILES, scope, root)
        self.permissions.grant(Capability.RUN_CHECKS, scope, root)
        return True

    # ================================================================ ingest
    def _nvim_hello(self, ident: ClientIdentity) -> dict[str, Any]:
        obs = self.config.observation
        return {
            "paused": self.paused or not self.permissions.check(Capability.OBSERVE_EDITOR, self.workspace_root).allowed,
            "roots": [str(r) for r in self.scope.roots],
            "max_bytes": obs.max_file_bytes,
            "cursor_ms": obs.cursor_coalesce_ms,
            "observe_unnamed": obs.observe_unnamed_buffers,
            "telemetry": [self._telemetry_target(lid) for lid in self.telemetry_enabled],
        }

    def _on_ipc_connect(self, ident: ClientIdentity, connected: bool) -> None:
        if ident.kind == SourceKind.NVIM:
            log.info("neovim %s %s", ident.id, "connected" if connected else "disconnected")
            if not connected:
                src = self.state.sources.get((ident.kind.value, ident.id))
                if src is not None:
                    from ..state.projection import SyncStatus

                    src.status = SyncStatus.DISCONNECTED
                    src.detail = "Neovim disconnected"
                # Buffers from a closed editor are no longer live.
                for key in [k for k in self.state.buffers if k[0] == ident.id]:
                    del self.state.buffers[key]
                self.state.bump({"editor.buffers", "sync:nvim"})
                self.push_status()

    def _on_ipc_message(self, ident: ClientIdentity, msg: dict[str, Any]) -> None:
        if self.paused:
            self.dropped_while_paused += 1
            return  # discarded on arrival: not stored, not forwarded
        seq = msg.pop("seq", None)
        ts = msg.pop("ts", None)
        if not isinstance(seq, int) or seq < 0:
            return
        if msg.get("type") not in ALLOWED_CLIENT_TYPES.get(ident.kind, frozenset()):
            log.warning("IPC: %s may not send %r", ident.kind.value, msg.get("type"))
            return
        try:
            payload = PAYLOAD_ADAPTER.validate_python(msg)
        except ValidationError as exc:
            log.warning("IPC: invalid %s payload: %s", msg.get("type"), exc.error_count())
            return
        captured = float(ts) if isinstance(ts, int | float) else self.clock()
        self._enqueue(ident.source_ref(), seq, captured, payload, Provenance.OBSERVED)

    def _on_terminal(self, epoch: str, seq: int, payload: Payload, prov: Provenance) -> None:
        if self.paused:
            return
        source = SourceRef(kind=SourceKind.TMUX, id=f"tmux-{self.tmux_session}", epoch=epoch)
        self._enqueue(source, seq, self.clock(), payload, prov)

    def _enqueue(self, source: SourceRef, seq: int, captured: float, payload: Payload, prov: Provenance) -> None:
        env = EventEnvelope(
            session_id=self.session_id,
            source=source,
            seq=seq,
            captured_at=captured,
            received_at=self.clock(),
            provenance=prov,
            payload=payload,
        )
        try:
            self.queue.put_nowait(env)
        except asyncio.QueueFull:
            # Backpressure: drop this event, mark the source incomplete, and
            # resynchronise from a snapshot once we catch up.
            self.dropped_events += 1
            from ..state.projection import SyncStatus

            key = (source.kind.value, source.id)
            src = self.state.sources.get(key)
            if src is not None:
                src.status = SyncStatus.INCOMPLETE
                src.detail = "events dropped under load"
            self._request_resync(source.kind)

    async def _process_loop(self) -> None:
        while True:
            env = await self.queue.get()
            try:
                self._apply(env)
                while not self.queue.empty():
                    self._apply(self.queue.get_nowait())
                self._after_batch()
            except Exception:
                log.exception("failed to process event")

    def _apply(self, env: EventEnvelope) -> None:
        if self.paused:
            return
        p = env.payload
        # Defence in depth: re-check editor paths against the approved scope.
        if isinstance(p, NvimBufferSnapshot | NvimBufferWritten | NvimFocus):
            if not self.scope.check(p.path).allowed:
                self._rejected_buffers.add((env.source.id, p.buf))
                return
            self._rejected_buffers.discard((env.source.id, p.buf))
        elif hasattr(p, "buf") and (env.source.id, p.buf) in self._rejected_buffers:
            return
        verdict = self.sequencer.check(env.source, env.seq)
        if not verdict.accept:
            return
        self.ingest_index += 1
        env = env.model_copy(update={"ingest_index": self.ingest_index})
        result = self.state.apply(env, verdict)
        if result.resync:
            self._request_resync(env.source.kind)
        self._persist(env)
        self._hooks(env)

    def _persist(self, env: EventEnvelope) -> None:
        if env.type in NOT_PERSISTED:
            return
        payload = _redact_obj(env.payload.model_dump(mode="json"))
        self.store.conn.execute(
            "INSERT OR IGNORE INTO events(event_id, session_id, source_kind, source_id, seq, ingest_index, type, "
            "captured_at, received_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                env.event_id,
                env.session_id,
                env.source.kind.value,
                env.source.id,
                env.seq,
                env.ingest_index,
                env.type,
                env.captured_at,
                env.received_at,
                json.dumps(payload),
            ),
        )

    def _request_resync(self, kind: SourceKind) -> None:
        if self.paused:
            return
        if kind == SourceKind.NVIM and self.ipc is not None:
            self._spawn(self.ipc.send(SourceKind.NVIM, {"cmd": "resync"}))
        elif kind == SourceKind.TMUX and self.terminal is not None:
            self._spawn(self.terminal.snapshot("gap"))

    # ================================================================ hooks
    def exercise_dir(self) -> Path | None:
        lesson = self.lesson()
        return self.workspace_root / lesson.workdir if lesson else None

    def _in_exercise(self, path: str) -> bool:
        d = self.exercise_dir()
        if d is None:
            return False
        real = os.path.realpath(path)
        base = os.path.realpath(d)
        return real == base or real.startswith(base + os.sep)

    def _hooks(self, env: EventEnvelope) -> None:
        t = env.type
        is_tutor_output = isinstance(env.payload, TmuxOutput) and env.payload.pane_id == self.tutor_pane
        activity = ("tmux.output", "nvim.buffer_lines", "nvim.cursor", "nvim.mode", "shell.command_started")
        if t in activity and not is_tutor_output:
            self.last_activity = self.monotonic()
        if t == "shell.command_finished":
            self.last_activity = self.monotonic()
            record = self.state.last_command()
            if record is None:
                return
            relevant = self._in_exercise(record.cwd)
            if self.policy.note_command(record.exit_status or 0, relevant, record.command):
                self._consider_unsolicited(Trigger.REPEATED_FAILURE)
            if relevant and record.exit_status == 0:
                self._spawn(self.check(auto=True))
        elif t == "nvim.buffer_written":
            p = env.payload
            assert isinstance(p, NvimBufferWritten)
            if self._in_exercise(p.path):
                self._spawn(self.check(auto=True))

    def _after_batch(self) -> None:
        fl = self.inflight
        if fl is not None and not fl.solicited and not fl.task.done():
            s = assess(fl.trigger, fl.revision, self.state)
            if s.material:
                fl.cancel_status = "withdrawn"
                fl.cancel_note = f"Hint withdrawn: {s.reason}. Nice."
                fl.task.cancel()
        self.push_status()

    def _consider_unsolicited(self, trigger: Trigger) -> None:
        decision = self.policy.decide(
            trigger,
            mode=self.session.mode,
            style=self.profile.intervention_style(),
            response_in_flight=self.inflight is not None and not self.inflight.task.done(),
            shell_busy=bool(self.state.running),
            seconds_since_activity=self.monotonic() - self.last_activity,
            exercise_active=self.lesson() is not None,
        )
        log.info("policy %s: act=%s (%s)", trigger.value, decision.act, decision.reason)
        if decision.act:
            self.policy.mark_unsolicited()
            self._spawn(self._unsolicited_failure_turn())
        elif decision.wait > 0:
            if self._deferred is not None:
                self._deferred.cancel()
            loop = asyncio.get_running_loop()
            self._deferred = loop.call_later(decision.wait, self._consider_unsolicited, trigger)

    async def _unsolicited_failure_turn(self) -> None:
        lesson = self.lesson()
        level = max(1, self.session.hint_level)
        hint = lesson.hint(level) if lesson else None
        if lesson is not None:
            self._record_assistance(level, hint=True)
        await self.run_turn(Trigger.REPEATED_FAILURE, hint_text=hint.text if hint else None, hint_level=level)

    # ================================================================ status
    def status_info(self) -> StatusInfo:
        from ..state.projection import SyncStatus

        observation = "paused" if self.paused else ("on" if self.observing else "off")
        sources = {}
        for kind, label in (
            (SourceKind.TMUX, "term"),
            (SourceKind.SHELL, "shell"),
            (SourceKind.NVIM, "nvim"),
        ):
            st = self.state.source_status(kind)
            if kind == SourceKind.NVIM and self.ipc is not None and not self.ipc.connected(SourceKind.NVIM):
                st = SyncStatus.DISCONNECTED if st is not None else None
            sources[label] = "paused" if self.paused else (st.value if st else "none")
        perms = sorted({g.capability.value for g in self.permissions.active_grants()})
        return StatusInfo(
            goal=self.session.goal,
            exercise=self.session.active_exercise,
            stage=self.session.exercise_stage,
            mode=self.session.mode,
            observation=observation,
            sync=self.state.overall_sync().value if self.observing or self.paused else "off",
            sources=sources,
            provider=self.provider.name,
            model=self.provider.model,
            mock=not self.provider.makes_network_calls,
            permissions=perms,
            busy=self.inflight is not None and not self.inflight.task.done(),
            usage=self.sessions.usage_totals(self.session_id),
        )

    def push_status(self) -> None:
        info = self.status_info()
        self.ui.status(info)
        text = info.line()
        now = self.monotonic()
        if (
            self.tmux is not None
            and self.tmux_session
            and text != self._status_text
            and now - self._last_tmux_status > 0.5
        ):
            self._status_text = text
            self._last_tmux_status = now
            tmux, session = self.tmux, self.tmux_session
            self._spawn(
                asyncio.to_thread(
                    tmux.run,
                    "set-option",
                    "-t",
                    f"={session}",
                    "status-right",
                    text.replace("#", "##"),
                    check=False,
                )
            )

    # ================================================================ lessons
    def lesson(self) -> Lesson | None:
        lid = self.session.active_exercise
        return load_lessons().get(lid) if lid else None

    def _save_session(self) -> None:
        self.sessions.save_state(self.session)
        self.push_status()

    def _record_assistance(self, level: int, *, hint: bool = False, solution: bool = False) -> None:
        lesson = self.lesson()
        if lesson is None:
            return
        attempt = self.evidence.current_attempt(lesson.id) or self.evidence.start_attempt(
            self.session_id, lesson, self.session.mode
        )
        self.evidence.record_assistance(attempt.id, level, hint=hint, solution=solution)

    async def _approve(self, capability: Capability, resource: Path | str | None, title: str, body: str) -> bool:
        decision = self.permissions.check(capability, resource)
        if decision.allowed:
            self.permissions.consume(decision)
            return True
        if not decision.needs_approval:
            self.ui.note(decision.reason, "warn")
            return False
        answer = await self.ui.ask(title, body, APPROVE_CHOICES)
        if answer not in SCOPE_FOR:
            self.ui.note("Not approved. Nothing was done.", "info")
            return False
        grant = self.permissions.grant(capability, SCOPE_FOR[answer], resource)
        if SCOPE_FOR[answer] == Scope.ONCE:
            self.store.conn.execute("UPDATE permissions SET used_at = ? WHERE id = ?", (self.clock(), grant))
        return True

    def _telemetry_target(self, lesson_id: str) -> dict[str, str]:
        lesson = load_lessons()[lesson_id]
        file = next((s.path for s in lesson.setup if s.kind == "file"), "")
        return {
            "exercise_id": lesson_id,
            "path": os.path.realpath(self.workspace_root / lesson.workdir / file),
        }

    async def start_lesson(self, query: str | None = None, *, reset: bool = False) -> Lesson | None:
        if query:
            lesson = find_lesson(query)
            if lesson is None:
                self.ui.note(f"No lesson matches {query!r}. Try `/lesson list`.", "warn")
                return None
        else:
            lesson = next_lesson(self.evidence.completed_exercises())
            if lesson is None:
                self.ui.note("You have completed every built-in exercise. Try `/review`.", "success")
                return None
        if not self.scope.check(self.workspace_root).allowed:
            self.ui.note(f"The practice folder {self.workspace_root} is not an approved folder.", "error")
            return None
        plan = plan_setup(lesson, self.workspace_root, self.scope, reset=reset)
        if plan.to_create:
            approved = await self._approve(
                Capability.MODIFY_FILES,
                self.workspace_root,
                "Create exercise files?",
                f"To start “{lesson.title}” I will create these files on your Mac:\n\n{plan.describe()}\n\n"
                "Nothing outside this folder is touched. Existing files are kept.",
            )
            if not approved:
                return None
            # Re-check right before acting: did anything change while we asked?
            fresh = plan_setup(lesson, self.workspace_root, self.scope, reset=reset)
            if [p for p, _ in fresh.to_create] != [p for p, _ in plan.to_create]:
                self.ui.note(
                    "The folder changed while I was asking. Run /lesson again to see the new plan.",
                    "warn",
                )
                return None
            apply_setup(fresh, self.store)
        previous = self.lesson()
        if previous is not None and previous.id != lesson.id:
            self._disable_telemetry(previous.id)
        if lesson.telemetry == "nvim_normal_commands" and lesson.id not in self.telemetry_enabled:
            answer = await self.ui.ask(
                "Allow command counting for this exercise?",
                "This Neovim exercise can check HOW you edited, not just the result.\n"
                "If you allow it, Neovim counts which Normal-mode commands you use (for example `dw: 2`),\n"
                "only in this exercise's file, only until the exercise ends. It never records the text you type.\n"
                "If you say no, the result is still checked, and the technique is reported as 'unverified'.",
                [("y", "Yes, count commands"), ("n", "No thanks")],
            )
            if answer == "y":
                self.telemetry_enabled.add(lesson.id)
                if self.ipc is not None:
                    await self.ipc.send(
                        SourceKind.NVIM,
                        {"cmd": "telemetry", "enabled": True, **self._telemetry_target(lesson.id)},
                    )
        self.evidence.start_attempt(self.session_id, lesson, self.session.mode)
        self.evidence.introduce(lesson.skills)
        self.session.active_exercise = lesson.id
        self.session.exercise_stage = "explain"
        self.session.hint_level = 0
        self.policy.reset_failures()
        self._save_session()
        self.state.bump({"exercise"})
        if lesson.risk == "runs_learner_code":
            self.ui.note(
                "Heads up: checking this exercise runs your code on this Mac (not a sandbox). I will ask first.",
                "info",
            )
        await self.run_turn(Trigger.LESSON_INTRO)
        return lesson

    def _disable_telemetry(self, lesson_id: str) -> None:
        if lesson_id in self.telemetry_enabled:
            self.telemetry_enabled.discard(lesson_id)
            if self.ipc is not None:
                self._spawn(
                    self.ipc.send(
                        SourceKind.NVIM,
                        {"cmd": "telemetry", "enabled": False, "exercise_id": lesson_id},
                    )
                )

    async def answer_prediction(self, text: str) -> None:
        lesson = self.lesson()
        if lesson is None:
            return
        attempt = self.evidence.current_attempt(lesson.id)
        self.evidence.record_answer(attempt.id if attempt else None, lesson, "explanation", f"prediction: {text}")
        self.session.exercise_stage = "attempt"
        self._save_session()
        await self.run_turn(Trigger.PREDICTION, learner_text=text)

    async def skip_prediction(self) -> None:
        lesson = self.lesson()
        if lesson is None:
            return
        self.session.exercise_stage = "attempt"
        self._save_session()
        self.ui.note(f"**Your task:**\n\n{lesson.instructions.strip()}", "info")

    async def hint(self) -> None:
        lesson = self.lesson()
        if lesson is None:
            self.ui.note("No exercise is active. Start one with `/lesson`.", "info")
            return
        if self.session.hint_level >= lesson.max_hint_level:
            self.ui.note(
                "That was the last hint. `/solution` shows the full answer (it counts as full help).",
                "info",
            )
            return
        self.session.hint_level += 1
        if self.session.exercise_stage == "explain":
            self.session.exercise_stage = "attempt"
        self._save_session()
        hint = lesson.hint(self.session.hint_level)
        self._record_assistance(self.session.hint_level, hint=True)
        if self.session.mode == "assessment":
            self.ui.note("Assessment mode: this attempt is now marked as assisted.", "info")
        await self.run_turn(Trigger.HINT, hint_text=hint.text if hint else None, hint_level=self.session.hint_level)

    async def solution(self) -> None:
        lesson = self.lesson()
        if lesson is None or not lesson.solution:
            self.ui.note("No solution is available here.", "info")
            return
        answer = await self.ui.ask(
            "Show the full solution?",
            "Seeing the solution is fine, but this attempt will be recorded as 'full solution shown'.\n"
            "You can still type it yourself and run /check.",
            [("y", "Show it"), ("n", "Not yet")],
        )
        if answer != "y":
            return
        self._record_assistance(4, solution=True)
        self.ui.note(
            f"**Solution** (recorded as full help):\n\n```\n{lesson.solution}\n```\nNow type it yourself and run `/check`.",
            "info",
        )

    async def check(self, answer: str | None = None, *, auto: bool = False) -> VerificationResult | None:
        lesson = self.lesson()
        if lesson is None:
            if not auto:
                self.ui.note("No exercise is active. Start one with `/lesson`.", "info")
            return None
        if auto and self.session.exercise_stage in ("verified", "explain"):
            return None
        verifier = get_verifier(lesson.verifier)
        workdir = self.workspace_root / lesson.workdir
        runner: HostRunner | None = None
        if verifier.executes_learner_code:
            if auto:
                return None
            ok = await self._approve(
                Capability.RUN_LEARNER_CODE,
                workdir,
                "Run your code to check it?",
                "This check runs YOUR Python code on this Mac.\n"
                "Limits: 10 second timeout, CPU and file-size limits, a clean environment without API keys.\n"
                "It is host execution, NOT a sandbox: the code can do what your user account can do.",
            )
            if not ok:
                return None
            runner = HostRunner()
        elif not self.permissions.check(Capability.RUN_CHECKS, workdir).allowed:
            if auto:
                return None
            if not await self._approve(
                Capability.RUN_CHECKS,
                workdir,
                "Run the checker?",
                f"The checker reads files in {workdir} and your observed shell records.",
            ):
                return None
        attempt = self.evidence.current_attempt(lesson.id) or self.evidence.start_attempt(
            self.session_id, lesson, self.session.mode
        )
        ctx = VerifyContext(
            workdir=workdir,
            scope=self.scope,
            state=self.state,
            params=lesson.verifier_params,
            attempt_started_at=attempt.started_at,
            answer=answer,
            runner=runner,
            telemetry_enabled=lesson.id in self.telemetry_enabled,
            exercise_id=lesson.id,
        )
        result = await verifier.verify(ctx)
        if auto and not result.passed:
            return result  # stay quiet; do not record a "failure" the learner did not ask for
        self.evidence.record_verification(attempt.id, lesson, result)
        if result.passed:
            self.session.exercise_stage = "verified"
            self.session.hint_level = 0
            self.policy.reset_failures()
            self._disable_telemetry(lesson.id)
        self._save_session()
        self.state.bump({"exercise"})
        self.ui.note(result.summary(), "success" if result.passed else "warn")
        await self.run_turn(Trigger.VERIFICATION, verification=result)
        return result

    async def cleanup_lesson(self) -> None:
        lesson = self.lesson()
        if lesson is None:
            self.ui.note("No exercise is active.", "info")
            return
        if not await self._approve(
            Capability.MODIFY_FILES,
            self.workspace_root,
            "Remove exercise files?",
            "Only files I created and you have NOT changed will be removed.",
        ):
            return
        report = cleanup(lesson, self.workspace_root, self.store)
        self.ui.note(
            "\n".join(f"- {os.path.relpath(p, self.workspace_root)}: {a}" for p, a in report) or "Nothing to remove.",
            "info",
        )

    # ================================================================ turns
    async def greet(self) -> None:
        history = self.sessions.recent_messages(self.session_id, limit=1)
        lesson = self.lesson()
        due = self.evidence.due_reviews()
        if not history and lesson is None and not self.evidence.skill_states():
            self.ui.note(
                "Welcome to CLI Mentor. You do the work; I explain, hint, and check.\n"
                "Type `/lesson` to start the first exercise, or `/help` for all commands.",
                "info",
            )
            return
        lines = ["Welcome back."]
        if lesson is not None:
            lines.append(f"You were working on “{lesson.title}” (stage: {self.session.exercise_stage}).")
        if self.session.goal:
            lines.append(f"Your goal for this session: {self.session.goal}.")
        if due:
            lines.append("Skills due for review: " + ", ".join(s.skill for s in due[:5]) + ". Try `/review`.")
        lines.append("Type `/lesson` to continue or `/progress` to see your record.")
        self.ui.note(" ".join(lines), "info")

    async def cancel_turn(self, status: str = "cancelled", note: str = "") -> bool:
        fl = self.inflight
        if fl is None or fl.task.done():
            return False
        fl.cancel_status = status
        fl.cancel_note = note
        fl.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await fl.task
        return True

    async def learner_message(self, text: str) -> None:
        if self.session.exercise_stage == "explain" and self.lesson() is not None:
            await self.answer_prediction(text)
            return
        await self.run_turn(Trigger.LEARNER_MESSAGE, learner_text=text)

    async def run_turn(
        self,
        trigger: Trigger,
        *,
        learner_text: str = "",
        verification: VerificationResult | None = None,
        hint_text: str | None = None,
        hint_level: int | None = None,
        wait: bool = True,
    ) -> None:
        fl = self.inflight
        if fl is not None and not fl.task.done():
            if trigger in SOLICITED:
                await self.cancel_turn("superseded", "replaced by your newer request")
            else:
                return  # never overlap an unsolicited response
        inputs = TurnInputs(
            trigger=trigger,
            mode=self.session.mode,
            session_id=self.session_id,
            learner_text=learner_text,
            lesson=self.lesson(),
            stage=self.session.exercise_stage,
            hint_level=hint_level if hint_level is not None else self.session.hint_level,
            hint_text=hint_text,
            verification=verification,
            observation_allowed=not self.paused
            and self.observing
            and self.permissions.check(Capability.OBSERVE_TERMINAL).allowed,
            exercise_dir=self.exercise_dir(),
            goal=self.session.goal,
        )
        settings = self.config.provider_settings(self.provider.name)  # type: ignore[arg-type]
        ctx = self.assembler.build(
            inputs,
            max_context_tokens=settings.max_context_tokens,
            max_output_tokens=settings.max_output_tokens,
        )
        if learner_text:
            self.sessions.add_message(self.session_id, "learner", learner_text, trigger=trigger.value)
        self.turn_counter += 1
        turn_id = self.turn_counter
        task = asyncio.create_task(self._turn(turn_id, trigger, inputs, ctx))
        self.inflight = InFlight(turn_id, trigger, ctx.revision, task)
        self.push_status()
        if wait:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _meta(self, inputs: TurnInputs, ctx: AssembledContext) -> TurnMeta:
        lesson = inputs.lesson
        last = self.state.last_command()
        error = None
        if last is not None and last.exit_status:
            pane = next((p for p in self.state.panes.values() if p.tail or p.snapshot), None)
            if pane is not None:
                text = pane.tail if pane.tail.strip() else "\n".join(pane.snapshot)
                rows = [r for r in text.splitlines() if r.strip()]
                error = redact_text("\n".join(rows[-4:])) if rows else None
        return TurnMeta(
            trigger=inputs.trigger.value,
            learner_text=inputs.learner_text,
            mode=inputs.mode,
            exercise_id=lesson.id if lesson else None,
            exercise_title=lesson.title if lesson else None,
            stage=inputs.stage,
            explanation=(
                lesson.explanation_markdown()
                if inputs.trigger == Trigger.LESSON_INTRO
                else (
                    lesson.lookup_term(inputs.learner_text)
                    or (lesson.explanation_markdown() if not inputs.learner_text else None)
                )
                if inputs.trigger == Trigger.EXPLAIN
                else None
            )
            if lesson
            else None,
            prediction_question=lesson.prediction if lesson else None,
            instructions=lesson.instructions.strip() if lesson and inputs.trigger == Trigger.PREDICTION else None,
            hint_text=inputs.hint_text,
            hint_level=inputs.hint_level,
            last_command=redact_text(last.command) if last else None,
            last_exit=last.exit_status if last else None,
            error_excerpt=error if ctx.observation_included else None,
            verification_summary=inputs.verification.summary() if inputs.verification else None,
            observation_included=ctx.observation_included,
            extra={
                "passed": bool(inputs.verification and inputs.verification.passed),
                "expected_prediction": lesson.prediction_answer if lesson else "",
            },
        )

    async def _ensure_network(self) -> bool:
        if not self.provider.makes_network_calls:
            return True
        if self.permissions.check(Capability.NETWORK, self.provider.name).allowed:
            return True
        answer = await self.ui.ask(
            "Send context to the model provider?",
            f"Real tutor replies send your message and the context shown by /context to {self.provider.name}.\n"
            "Data sent cannot be recalled later.",
            [("s", "Yes, for this session"), ("p", "Yes, remember"), ("n", "No")],
        )
        if answer not in ("s", "p"):
            return False
        self.permissions.grant(Capability.NETWORK, SCOPE_FOR[answer], self.provider.name)
        return True

    async def _turn(self, turn_id: int, trigger: Trigger, inputs: TurnInputs, ctx: AssembledContext) -> None:
        label = MOCK_LABEL if not self.provider.makes_network_calls else f"{self.provider.name} · {self.provider.model}"
        meta = self._meta(inputs, ctx)
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        status = "complete"
        note = ""
        self.ui.tutor_begin(turn_id, trigger.value, label)
        try:
            if not await self._ensure_network():
                raise ProviderError("not_configured", "Sending to the provider was not approved.")
            settings = self.config.provider_settings(self.provider.name)  # type: ignore[arg-type]
            request = ProviderRequest(
                model=self.provider.model,
                system=ctx.system,
                messages=ctx.messages,
                max_output_tokens=settings.max_output_tokens,
                tools=actions.TOOLS,
                meta=meta,
            )
            async with asyncio.timeout(settings.timeout_seconds * 2 + 5):
                async for event in self.provider.stream(request):
                    if isinstance(event, TextDelta):
                        text_parts.append(event.text)
                        self.ui.tutor_delta(turn_id, event.text)
                    elif isinstance(event, ToolCall):
                        tool_calls.append(event)
                    elif isinstance(event, Usage):
                        self.sessions.add_usage(
                            self.session_id,
                            self.provider.name,
                            self.provider.model,
                            event.input_tokens,
                            event.output_tokens,
                            event.estimated,
                        )
                    elif isinstance(event, Done):
                        pass
            s = assess(trigger, ctx.revision, self.state)
            if trigger in SOLICITED and s.material and trigger not in (Trigger.LESSON_INTRO,):
                status = "stale"
                note = f"Heads up: {s.reason}, so this answer may be out of date. Ask again for a fresh look."
        except asyncio.CancelledError:
            fl = self.inflight
            status = fl.cancel_status if fl and fl.turn_id == turn_id else "cancelled"
            note = fl.cancel_note if fl and fl.turn_id == turn_id else ""
            self.ui.tutor_end(turn_id, status, note)
            self.sessions.add_message(
                self.session_id,
                "tutor",
                "".join(text_parts),
                trigger=trigger.value,
                status=status,
                revision=ctx.revision.revision,
            )
            self.push_status()
            raise
        except (ProviderError, TimeoutError) as exc:
            err = exc if isinstance(exc, ProviderError) else ProviderError("timeout", "The tutor took too long.")
            status = "error"
            note = err.user_message() + " Your workspace is untouched."
            self.ui.tutor_end(turn_id, status, note)
            self.sessions.add_message(
                self.session_id,
                "note",
                f"provider error: {err.kind}",
                trigger=trigger.value,
                status="error",
            )
            if trigger not in (Trigger.LEARNER_MESSAGE, Trigger.SESSION_START):
                self.ui.note(
                    "**Lesson text (shown without the AI because the provider failed):**\n\n" + compose(meta),
                    "info",
                )
            self.push_status()
            return
        self.ui.tutor_end(turn_id, status, note)
        self.sessions.add_message(
            self.session_id,
            "tutor",
            "".join(text_parts),
            trigger=trigger.value,
            status=status,
            revision=ctx.revision.revision,
            provider=self.provider.name,
            model=self.provider.model,
        )
        for call in tool_calls:
            await self._handle_tool(call, trigger)
        self.push_status()

    async def _handle_tool(self, call: ToolCall, trigger: Trigger) -> None:
        parsed = actions.validate(call)
        if parsed is None:
            log.warning("dropped invalid tool call %s", call.name)
            return
        if isinstance(parsed, actions.ProposeMemory):
            item = self.notes.add(
                parsed.text,
                category=parsed.category,
                provenance="model_inferred",
                source="tutor suggestion",
                session_id=self.session_id,
            )
            self.ui.note(
                f"The tutor suggests remembering: “{parsed.text}”. It is NOT stored as a fact unless you "
                f"confirm: `/memory confirm {item}` or `/memory reject {item}`.",
                "info",
            )
        elif isinstance(parsed, actions.RequestCheck):
            lesson = self.lesson()
            if trigger != Trigger.LEARNER_MESSAGE or lesson is None:
                return
            if get_verifier(lesson.verifier).executes_learner_code:
                self.ui.note(
                    "The tutor suggests running `/check` (it runs your code, so you start it).",
                    "info",
                )
            else:
                self._spawn(self.check())

    # ================================================================ observation control
    async def pause_observation(self) -> None:
        if self.paused:
            self.ui.note("Observation is already paused.", "info")
            return
        self.paused = True
        self.state.paused = True
        if self.terminal is not None:
            await self.terminal.stop()
            self.terminal = None
        if self.ipc is not None:
            await self.ipc.send(SourceKind.NVIM, {"cmd": "pause"})
        while not self.queue.empty():
            self.queue.get_nowait()
        fl = self.inflight
        if fl is not None and not fl.solicited:
            await self.cancel_turn("withdrawn", "observation paused")
        self.ui.note(
            "Observation PAUSED. Nothing new is recorded or sent to the model. "
            "Anything already sent to a provider cannot be recalled.",
            "info",
        )
        self.push_status()

    async def resume_observation(self) -> None:
        if not self.paused and self.observing:
            self.ui.note("Observation is already on.", "info")
            return
        if not self.paused:
            if not await self._ensure_observation_consent():
                return
        else:
            answer = await self.ui.ask(
                "Resume observation?",
                "I will take a FRESH snapshot of the shell screen and your open Neovim buffers.\n"
                "Nothing that happened during the pause is replayed.",
                [("y", "Yes, resume"), ("n", "Stay paused")],
            )
            if answer != "y":
                return
        self.paused = False
        self.state.paused = False
        self.state.shell_history_incomplete = True
        await self._start_terminal("resume")
        self.observing = self.terminal is not None or self.tmux is None
        if self.ipc is not None:
            await self.ipc.send(SourceKind.NVIM, {"cmd": "resume"})
        self.ui.note(
            "Observation resumed with fresh snapshots. Commands run during the pause were not seen.",
            "info",
        )
        self.push_status()

    # ================================================================ quitting
    def unsaved_summary(self) -> list[str]:
        out = [f"Neovim buffer with unsaved changes: {b.path}" for b in self.state.unsaved_buffers()]
        out += [f"Running command in the shell: {c.command}" for c in self.state.running.values()]
        return out
