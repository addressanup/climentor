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
import re
import shlex
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..config import Config
from ..curriculum.model import Lesson
from ..curriculum.plans import (
    PLAN_TOOL,
    STEP_TOOL,
    PlanInfo,
    PlanOutline,
    PlanStore,
    StepDetails,
    StepInfo,
    build_lesson,
    lesson_id,
    render_plan,
)
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
    ToolSpec,
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
NO_STALE_NOTE = frozenset({Trigger.LESSON_INTRO, Trigger.PLAN, Trigger.PLAN_REVISE, Trigger.PREPARE_STEP})
# Every reply gets room to finish: wordy models (DeepSeek) cut off explanations and hints at 1024 tokens.
MIN_REPLY_TOKENS = 3000
CUT_OFF = frozenset({"length", "max_tokens", "max_output_tokens"})  # stop reasons that mean “ran out of room”
CALLER_HANDLED_TOOLS = frozenset({"propose_plan", "prepare_step", "assess_step", "request_check", "start_step"})
YES = re.compile(
    r"^(y|yes|yeah|yep|ok|okay|sure|start|go|let'?s go|let'?s start|sounds good|looks good|perfect)\b[.!]*", re.I
)
# Plain-English requests the engine acts on directly (short messages only). Everything else goes to the
# tutor model, which can take the same actions through its tools (request_check, assess_step, start_step).
SHORT_WORDS = 8
DONE = re.compile(
    r"^((i'?m|i am|i think i'?m|i think it'?s|it'?s|it is|all|that'?s)\s+)?"
    r"(done|finished|complete|ready to check|working now)\b"
    r"|^(check|check it|check this|check my work|check my answer|did it|i did it|that'?s it)\b",
    re.I,
)
NEXT = re.compile(
    r"^(next|next step|next one|continue|go on|go ahead|ready|i'?m ready|let'?s go|let'?s continue|let'?s start"
    r"|start|start it|begin|ok|okay|yes|yep|yeah|sure|go|move on|let'?s move on)[.!]*$",
    re.I,
)
HINT = re.compile(
    r"^((a|another|one more|give me a|can i get a|can i have a|i need a)\s+)?hint\b|\bi'?m stuck\b|\bi am stuck\b"
    r"|^stuck\b",
    re.I,
)
SKIP = re.compile(r"^skip(\s+(this|the|it))?(\s+(step|question|prediction))?[.!]*$", re.I)
SOLUTION = re.compile(r"\b(show|give|tell)( me)? the (solution|answer)\b", re.I)
SHOW_PLAN = re.compile(r"\b(show|see|view)( me)? (the |my )?plan\b", re.I)
NEW_SUBJECT = re.compile(r"^new (subject|topic)\b[:,]?\s*(?P<topic>.*)$", re.I)
APPROVE_CHOICES = [("y", "Yes, this time"), ("s", "Yes, for this session"), ("n", "No")]
SCOPE_FOR = {"y": Scope.ONCE, "s": Scope.SESSION, "p": Scope.PERSISTENT}

TOUR = """\
**Welcome to CLI Mentor.** I am your tutor. You learn by doing real work in real tools, \
and I help from this pane.

**Your screen**
- **Left: me.** Type here to talk to me.
- **Top right: Neovim**, for writing code.
- **Bottom right: the shell**, for running it.

**Moving around**
- Click a pane, or press **Ctrl+b**, then an **arrow key**.
- **Ctrl+b**, then **z** makes a pane full size. Do it again to go back.

**Talking to me**
- Just type in plain English, then press **Enter**. **Ctrl+J** starts a new line.
- You never need commands. Say **done** when you finish a task, **hint** if you are stuck, \
**next** to move on, or ask me anything.

**How we work:** I explain new words and symbols first. Then you do the task yourself. \
When you tell me you are done (or answer my question), I check your work with real tests. \
If you run your program in the shell, I may notice it works and check it by myself."""

GOAL_QUESTION = """\
**What would you like to learn?** Say it in your own words. For example:

- C++ from the very basics to the most advanced features
- Python for automating everyday tasks
- Git, from the first commit to rebasing

I will turn it into a step-by-step plan, and you can change the plan before we start."""


def _check_types(lesson: Lesson) -> set[str]:
    return {str(c.get("type")) for c in lesson.verifier_params.get("checks", []) if isinstance(c, dict)}


def _has_answer_check(lesson: Lesson) -> bool:
    return "learner_answer" in _check_types(lesson)


def _needs_review(lesson: Lesson) -> bool:
    """No automatic check: the tutor judges the work (recorded as unverified)."""
    return not _check_types(lesson)


def _problems(exc: ValidationError, limit: int = 6) -> str:
    """Where a model's tool call broke the schema: field paths and reasons, never the values."""
    found = exc.errors(include_input=False, include_url=False)
    return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'top level'}: {e['msg']}" for e in found[:limit])


def _retry_note(tool: str, problem: str) -> str:
    return f"Your last {tool} call was rejected: {problem}. Call {tool} again with every field valid and complete."


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
        self.plans = PlanStore(store)
        self._turn_tools: dict[int, list[ToolCall]] = {}
        self.last_reply = ""  # words of the latest visible tutor reply
        self.assembler = ContextAssembler(
            self.state, self.scope, self.profile, self.notes, self.evidence, self.sessions, clock, plans=self.plans
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
        self._tmux_status_wanted = ""
        self._tmux_status_timer: asyncio.TimerHandle | None = None
        self._last_tmux_status = -1.0e9  # long ago, so the first update goes out at once
        self.socket_path: Path | None = None
        self.token_file: Path | None = None

    # ================================================================ lifecycle
    async def start(self, *, observe: bool = True, greet: bool = True) -> None:
        purge_expired(self.store, self.config)
        self._processor = asyncio.create_task(self._process_loop(), name="mentor-processor")
        self.push_status()
        if greet and self._first_run():
            self.show_tour()
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
        if self._tmux_status_timer is not None:
            self._tmux_status_timer.cancel()
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
            "May I watch this session?",
            "To help, I need to see your work in this session:\n"
            "- the shell pane: the commands you run and what they print\n"
            f"- Neovim: files inside {root}, even before you save\n\n"
            "I cannot see your other windows or terminals. Commands that start with a space stay private. "
            "I filter out secrets such as passwords and keys (best effort).\n"
            "You can pause any time with /observe pause.",
            [("s", "Yes, for this session"), ("p", "Yes, and remember"), ("n", "No")],
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
                source_key = (ident.kind.value, ident.id)
                others = [k for k in self.state.sources if k[0] == ident.kind.value and k != source_key]
                src = self.state.sources.get(source_key)
                if others:  # another Neovim is still open (say, one started in the shell just closed)
                    self.state.sources.pop(source_key, None)
                elif src is not None:
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
                self._spawn(self.check(auto=True, run_code=True))
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
        plan = self.plans.get(self.session.plan_id) if self.session.plan_id else None
        done, total = self.plans.progress(plan.id) if plan else (0, 0)
        step = self.plans.current(plan) if plan and plan.status != "proposed" else None
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
            phase=self.session.phase,
            subject=plan.title if plan else None,
            step=step.position if step else None,
            steps=total,
            done=done,
            step_title=step.title if step else None,
            step_stage=(
                self.session.exercise_stage
                if plan and step and self.session.active_exercise == lesson_id(plan.id, step.step_id)
                else None
            ),
        )

    def push_status(self) -> None:
        info = self.status_info()
        self.ui.status(info)
        self._tmux_status_wanted = info.line()
        self._sync_tmux_status()

    def _sync_tmux_status(self) -> None:
        """Copy the status to the tmux bar, at most twice a second; a skipped update is sent later."""
        text = self._tmux_status_wanted
        if self.tmux is None or not self.tmux_session or text == self._status_text:
            return
        wait = 0.5 - (self.monotonic() - self._last_tmux_status)
        if wait > 0:
            if self._tmux_status_timer is None:
                with contextlib.suppress(RuntimeError):  # no event loop: nothing to schedule on
                    self._tmux_status_timer = asyncio.get_running_loop().call_later(wait, self._tmux_timer_fired)
            return
        self._status_text = text
        self._last_tmux_status = self.monotonic()
        tmux, session = self.tmux, self.tmux_session
        self._spawn(
            asyncio.to_thread(
                tmux.run,
                "set-option",
                "-t",
                f"={session}:",  # tmux 3.7 rejects a bare "=name" here; "=name:" works
                "status-right",
                text.replace("#", "##"),
                check=False,
            )
        )

    def _tmux_timer_fired(self) -> None:
        self._tmux_status_timer = None
        self._sync_tmux_status()

    # ================================================================ lessons
    def lesson(self) -> Lesson | None:
        lid = self.session.active_exercise
        return self.plans.lesson(lid) if lid else None

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
        lesson = self.plans.lesson(lesson_id)
        if lesson is None:
            return {"exercise_id": lesson_id, "path": ""}
        file = next((s.path for s in lesson.setup if s.kind == "file"), "")
        return {
            "exercise_id": lesson_id,
            "path": os.path.realpath(self.workspace_root / lesson.workdir / file),
        }

    async def start_lesson(self, lesson: Lesson, *, reset: bool = False) -> Lesson | None:
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
                    "The folder changed while I was asking. Say “next” again and I will look once more.",
                    "warn",
                )
                return None
            apply_setup(fresh, self.store)
        else:  # no starter files, but the learner still works in this step's own folder
            plan.workdir.mkdir(parents=True, exist_ok=True)
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

    # ================================================================ plans
    def _can_plan(self) -> bool:
        if getattr(self.provider, "can_plan", True):
            return True
        self._say(
            "Making a learning plan needs a real AI model, and the offline mock tutor cannot design one. "
            "Connect a model with `mentor init` (your ChatGPT plan, Anthropic, OpenAI, or an OpenAI-compatible "
            "service such as DeepSeek), then start a new session with `mentor start`.",
            "plan",
        )
        return False

    async def _ask_outline(self, trigger: Trigger, text: str, extra: list[str]) -> PlanOutline | None:
        calls = await self.run_turn(
            trigger, learner_text=text, tools=[PLAN_TOOL], extra_trusted=extra, plan_budget=True
        )
        for attempt in range(2):
            problem = ""
            for call in calls:
                if call.name == "propose_plan":
                    try:
                        return PlanOutline.model_validate(call.arguments)
                    except ValidationError as exc:
                        problem = _problems(exc)
                        log.warning("invalid plan from model: %s", problem)
            if not problem or attempt:  # no plan offered (perhaps a question for the learner), or a second miss
                break
            calls = await self.run_turn(
                trigger,
                tools=[PLAN_TOOL],
                extra_trusted=[*extra, _retry_note("propose_plan", problem)],
                plan_budget=True,
                visible=False,
            )
        self.ui.note(
            "I could not get a usable plan from the model. Try saying it again, a bit more specifically.", "warn"
        )
        return None

    async def create_plan(self, goal: str) -> PlanInfo | None:
        if not self._can_plan():
            return None
        self.session.phase = "ask_goal"
        outline = await self._ask_outline(Trigger.PLAN, goal, [])
        if outline is None:
            return None
        plan = self.plans.create(goal, outline, self.provider.name, self.provider.model, session_id=self.session_id)
        self.session.plan_id = plan.id
        self.session.phase = "plan_review"
        self._save_session()
        done, total = 0, sum(len(m.steps) for m in outline.modules)
        self._say(
            render_plan(outline) + f"\n\nThat is {total} steps, one small idea at a time. Reply **yes** to "
            "start, or tell me what to change (for example “skip the basics I know” or “more on templates”).",
            "plan",
        )
        _ = done
        return plan

    async def revise_plan(self, feedback: str) -> PlanInfo | None:
        plan = self.plans.get(self.session.plan_id) if self.session.plan_id else None
        if plan is None or plan.status != "proposed":
            await self.create_plan(feedback)
            return None
        outline = await self._ask_outline(
            Trigger.PLAN_REVISE, feedback, ["Current proposed plan:\n" + render_plan(plan.outline)]
        )
        if outline is None:
            return None
        plan = self.plans.revise(plan.id, outline)
        self._save_session()
        self._say(
            "Here is the updated plan:\n\n"
            + render_plan(outline)
            + "\n\nReply **yes** to start, or tell me what else to change.",
            "plan",
        )
        return plan

    async def accept_plan(self) -> None:
        plan = self.plans.get(self.session.plan_id) if self.session.plan_id else None
        if plan is None:
            self.ui.note("There is no proposed plan. Tell me what you want to learn.")
            return
        plan = self.plans.activate(plan.id)
        self.plans.touch(plan.id, self.session_id)
        self.session.phase = "learning"
        self._save_session()
        step = self.plans.current(plan)
        self._say(
            f"Great, the plan **{plan.title}** is saved. Ask me to show the plan any time. "
            + (f"Let's begin with step 1: **{step.title}**." if step else ""),
            "plan",
        )
        await self.start_step()

    async def new_goal(self, goal: str) -> None:
        self.session.phase = "ask_goal"
        self.session.plan_id = None
        self._save_session()
        if goal.strip():
            await self.create_plan(goal)
        else:
            self._say(GOAL_QUESTION, "plan")

    def _toolchain(self) -> str:
        tools = [
            t for t in ("c++", "cc", "python3", "make", "lldb", "git", "node", "go", "rustc", "java") if shutil.which(t)
        ]
        return "Programs available on the learner's Mac: " + (", ".join(tools) or "none detected")

    async def _prepare_step(self, plan: PlanInfo, step: StepInfo) -> Lesson | None:
        steps = self.plans.steps(plan.id)
        done = [f"{st.position}. {st.title}" for st in steps if st.position < step.position]
        later = [f"{st.position}. {st.title}" for st in steps if st.position > step.position][:5]
        extra = [
            f"Plan: {plan.title}. The learner's goal in their words: {plan.goal[:300]}",
            f"Step to prepare: {step.position} of {len(steps)} (module “{step.module}”): {step.title}",
            f"Objective: {step.objective}",
            "New concepts for this step: " + (", ".join(step.concepts) or "(choose them)"),
            "Earlier steps: " + ("; ".join(done[-8:]) or "none (this is the first step)"),
            "Later steps (do not teach these yet): " + ("; ".join(later) or "none"),
            self._toolchain(),
            "Practice files are created in a fresh folder for this step; paths are relative to it.",
        ]
        self.ui.note(f"Preparing step {step.position}: {step.title}…", "info")
        notes: list[str] = []
        for _attempt in range(2):  # a rejected step gets one retry, told exactly what was wrong
            calls = await self.run_turn(
                Trigger.PREPARE_STEP, tools=[STEP_TOOL], extra_trusted=extra + notes, visible=False, plan_budget=True
            )
            problem = "you did not call prepare_step"
            for call in calls:
                if call.name == "prepare_step":
                    try:
                        lesson = build_lesson(plan, step, StepDetails.model_validate(call.arguments))
                    except ValidationError as exc:
                        problem = _problems(exc)
                        log.warning("invalid step from model: %s", problem)
                        continue
                    self.plans.save_lesson(plan.id, step.step_id, lesson)
                    return lesson
            notes = [_retry_note("prepare_step", problem)]
        self.ui.note("I could not prepare this step. Say “next” to try again.", "warn")
        return None

    async def start_step(self, *, reset: bool = False) -> Lesson | None:
        plan = self.plans.active()
        if plan is None:
            if self.session.phase == "plan_review":
                self.ui.note("Reply **yes** to accept the proposed plan first, or tell me what to change.")
            else:
                await self.new_goal("")
            return None
        self.session.plan_id = plan.id
        step = self.plans.current(plan)
        if step is None:
            self._say(
                f"Every step of **{plan.title}** is done. Ask me for a review, or say “new subject:” and a topic "
                "to learn something new."
            )
            return None
        current = self.lesson()
        if current is not None and step.lesson is not None and current.id == step.lesson.id and not reset:
            if self.session.exercise_stage == "explain":
                await self.run_turn(Trigger.LESSON_INTRO)
            elif self.session.exercise_stage != "verified":
                self.ui.note(
                    f"Continuing step {step.position}: **{step.title}**.\n\n**Your task:**\n\n"
                    + current.instructions.strip()
                )
            return current
        if not self._can_plan() and step.lesson is None:
            return None
        lesson = step.lesson or await self._prepare_step(plan, step)
        if lesson is None:
            return None
        return await self.start_lesson(lesson, reset=reset)

    async def skip_step(self) -> None:
        plan = self.plans.active()
        step = self.plans.current(plan) if plan else None
        if plan is None or step is None:
            self.ui.note("There is no current step to skip.")
            return
        lesson = self.lesson()
        if lesson is not None:
            attempt = self.evidence.current_attempt(lesson.id)
            if attempt is not None:
                self.evidence.close_attempt(attempt.id, "abandoned")
        nxt = self.plans.finish_step(plan.id, step.step_id, "skipped")
        self.session.exercise_stage = "verified"
        self._save_session()
        self._say(
            f"Skipped step {step.position}: {step.title}."
            + (
                f" Next is **step {nxt.position}: {nxt.title}**. Say **next** when you are ready."
                if nxt
                else " That was the last step."
            ),
            "plan",
        )

    def plan_markdown(self) -> str:
        plan = self.plans.get(self.session.plan_id) if self.session.plan_id else self.plans.active()
        if plan is None:
            return "No plan yet. Tell me what you want to learn."
        done, total = self.plans.progress(plan.id)
        head = f"Status: **{plan.status}** · {done} of {total} steps done · your goal: “{plan.goal[:200]}”\n\n"
        return head + render_plan(plan.outline, self.plans.steps(plan.id), plan.current_step)

    async def switch_plan(self, prefix: str) -> None:
        plan = self.plans.find(prefix)
        if plan is None:
            self.ui.note("No plan matches that id. See `/plan list`.", "warn")
            return
        plan = self.plans.activate(plan.id)
        self.plans.touch(plan.id, self.session_id)
        self.session.plan_id = plan.id
        self.session.phase = "learning"
        self._save_session()
        self.ui.note(f"Switched to **{plan.title}**. Say “next” to continue.", "success")

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
        self._where_to_work()

    async def skip_prediction(self) -> None:
        lesson = self.lesson()
        if lesson is None:
            return
        self.session.exercise_stage = "attempt"
        self._save_session()
        self.ui.note(f"**Your task:**\n\n{lesson.instructions.strip()}", "info")
        self._where_to_work()

    def _where_to_work(self) -> None:
        """Say which folder the task's files belong in, unless the shell is already there."""
        wd = self.exercise_dir()
        if wd is None or (self.state.cwd and os.path.realpath(self.state.cwd) == os.path.realpath(wd)):
            return
        self.ui.note(f"**Where to work:** this step's folder. In the shell, run `cd {shlex.quote(str(wd))}` first.")

    async def hint(self) -> None:
        lesson = self.lesson()
        if lesson is None:
            self.ui.note("There is no task right now, so there is no hint. Say “next” to start the next step.", "info")
            return
        if self.session.hint_level >= lesson.max_hint_level:
            self.ui.note(
                "That was the last hint. Say “show me the solution” if you want the full answer "
                "(it counts as full help).",
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
            "You can still type it yourself, then tell me when you are done.",
            [("y", "Show it"), ("n", "Not yet")],
        )
        if answer != "y":
            return
        self._record_assistance(4, solution=True)
        self.ui.note(
            f"**Solution** (recorded as full help):\n\n```\n{lesson.solution}\n```\nNow type it yourself, then tell me when you are done.",
            "info",
        )

    async def check(
        self, answer: str | None = None, *, auto: bool = False, run_code: bool = False
    ) -> VerificationResult | None:
        """Run the trusted checks. ``auto``: nobody asked, so never ask questions and stay quiet on failure.
        ``run_code``: the learner just ran their program; an automatic check may run it too if they allowed
        that for the session."""
        lesson = self.lesson()
        if lesson is None:
            if not auto:
                self.ui.note("There is no task to check right now. Say “next” to start the next step.", "info")
            return None
        if auto and self.session.exercise_stage in ("verified", "explain"):
            return None
        verifier = get_verifier(lesson.verifier)
        workdir = self.workspace_root / lesson.workdir
        # “For this session” must cover every step of the subject, not only this step's folder.
        code_scope = workdir.parent if lesson.track == "plan" else workdir
        runner: HostRunner | None = None
        if verifier.executes_learner_code or lesson.risk == "runs_learner_code":
            if auto:
                decision = self.permissions.check(Capability.RUN_LEARNER_CODE, code_scope)
                if not (run_code and decision.allowed):
                    return None
                self.permissions.consume(decision)
                ok = True
            else:
                ok = await self._approve(
                    Capability.RUN_LEARNER_CODE,
                    code_scope,
                    "Run your code to check it?",
                    "This check compiles (if needed) and runs YOUR code on this Mac.\n"
                    "Limits: 10 second timeout, CPU and file-size limits, a clean environment without API keys.\n"
                    "It is host execution, NOT a sandbox: the code can do what your user account can do.\n\n"
                    "Choose “for this session” and I will also check by myself each time you run your program.",
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
        if result.needs_review:
            self.ui.note(result.summary(), "info")
            await self._review_step(lesson, attempt.id)
            return result
        self.evidence.record_verification(attempt.id, lesson, result)
        self.state.bump({"exercise"})
        self.ui.note(result.summary(), "success" if result.passed else "warn")
        if result.passed:
            self._mark_verified(lesson)
        else:
            self._save_session()
        await self.run_turn(Trigger.VERIFICATION, verification=result)
        if result.passed:
            self._announce_next(lesson)
        return result

    def _mark_verified(self, lesson: Lesson) -> None:
        self.session.exercise_stage = "verified"
        self.session.hint_level = 0
        self.policy.reset_failures()
        self._disable_telemetry(lesson.id)
        self._save_session()
        self._finish_plan_step(lesson, "done")

    def _finish_plan_step(self, lesson: Lesson, status: str) -> StepInfo | None:
        plan = self.plans.get(self.session.plan_id) if self.session.plan_id else None
        if plan is None or not lesson.id.endswith("." + (plan.current_step or "-")):
            return None
        return self.plans.finish_step(plan.id, plan.current_step or "", status)

    def _announce_next(self, lesson: Lesson) -> None:
        plan = self.plans.get(self.session.plan_id) if self.session.plan_id else None
        if plan is None:
            return
        done, total = self.plans.progress(plan.id)
        nxt = self.plans.current(plan)
        if nxt is None:
            self._say(
                f"That was the last step of **{plan.title}** ({done} of {total} done). Well done! "
                "Ask me for a review any time, or say “new subject:” and a topic to learn something else.",
                "verification",
            )
        else:
            self._say(
                f"Step done ({done} of {total}). Next is **step {nxt.position}: {nxt.title}** — "
                f"{nxt.objective.rstrip('. ')}. Say **next** when you are ready, or ask me anything first.",
                "verification",
            )

    async def _review_step(self, lesson: Lesson, attempt_id: str) -> None:
        """No automatic check: the tutor reviews the observed work (recorded as unverified)."""
        calls = await self.run_turn(Trigger.REVIEW_STEP, tools=[actions.ASSESS_TOOL])
        verdict = next((v for v in (actions.validate(c) for c in calls) if isinstance(v, actions.AssessStep)), None)
        if verdict is None:
            self.ui.note(
                "The tutor did not give a clear verdict. Tell me again when you think it is done, or say "
                "“skip this step”.",
                "warn",
            )
            return
        self._apply_verdict(lesson, attempt_id, verdict)

    def _apply_verdict(self, lesson: Lesson, attempt_id: str, verdict: actions.AssessStep) -> None:
        self.evidence.record_answer(
            attempt_id, lesson, "review", f"{verdict.verdict}: {verdict.feedback}", provenance="model_inferred"
        )
        if verdict.verdict == "complete":
            self.evidence.close_attempt(attempt_id, "passed")
            self.ui.note("The tutor reviewed this step as complete (recorded as *unverified*).", "success")
            self._mark_verified(lesson)
            self.state.bump({"exercise"})
            self._announce_next(lesson)

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
    def _say(self, text: str, trigger: str = "session_start", *, store: bool = True) -> None:
        """A deterministic tutor message (no model call); stored so the model sees it later."""
        self.turn_counter += 1
        self.ui.tutor_begin(self.turn_counter, trigger, "CLI Mentor")
        self.ui.tutor_delta(self.turn_counter, text)
        self.ui.tutor_end(self.turn_counter, "complete")
        if store:
            self.sessions.add_message(self.session_id, "tutor", text, trigger=trigger)

    def _first_run(self) -> bool:
        """True only for the very first session: no other sessions, no plans, nothing said yet."""
        row = self.store.conn.execute(
            "SELECT (SELECT COUNT(*) FROM sessions WHERE id != ?) + (SELECT COUNT(*) FROM learning_plans) "
            "+ (SELECT COUNT(*) FROM messages WHERE session_id = ?)",
            (self.session_id, self.session_id),
        ).fetchone()
        return int(row[0]) == 0

    def show_tour(self) -> None:
        """The short walkthrough of the screen (first session, or `/tour`). Not sent to the model."""
        self._say(TOUR, "tour", store=False)

    def _other_subjects_note(self) -> str:
        others = self.plans.subjects()
        if not others:
            return ""
        names = ", ".join(f"“{p.title}”" for p in others[:4])
        return (
            f"\n\nYou also have saved subjects: {names}. To continue one, quit with Ctrl+Q and run "
            "`mentor start` in your terminal; it lets you pick one."
        )

    async def greet(self) -> None:
        mine = self.plans.get(self.session.plan_id) if self.session.plan_id else None
        proposed = mine if mine is not None and mine.status == "proposed" else None
        plan = mine if mine is not None and mine.status != "proposed" else None
        if plan is not None and plan.status == "archived":
            plan = self.plans.activate(plan.id)  # this session's subject becomes the one being studied
        if proposed is not None and proposed.status == "proposed":
            self.session.phase = "plan_review"
            self._save_session()
            self._say(
                "Welcome back! Here is the plan I proposed last time:\n\n"
                + render_plan(proposed.outline)
                + "\n\nReply **yes** to start, or tell me what to change."
            )
            return
        if plan is None:
            self.session.phase = "ask_goal"
            self._save_session()
            self._say(GOAL_QUESTION + self._other_subjects_note())
            return
        self.session.plan_id = plan.id
        self.session.phase = "learning"
        self._save_session()
        self.plans.touch(plan.id, self.session_id)
        done, total = self.plans.progress(plan.id)
        step = self.plans.current(plan)
        where = f"next is **step {step.position} of {total}: {step.title}**" if step else "every step is done"
        due = self.evidence.due_reviews()
        extra = f" Skills due for review: {', '.join(x.skill for x in due[:4])} (ask me for a review)." if due else ""
        self._say(
            f"Welcome back! You are learning **{plan.title}** ({done} of {total} steps done); {where}.{extra}"
            "\n\nSay **continue** when you are ready, or ask me anything first."
        )

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
        """Everything the learner types that is not a /shortcut. Plain English drives the whole lesson."""
        said = text.strip()
        short = len(said.split()) <= SHORT_WORDS
        if self.session.phase == "ask_goal":
            await self.create_plan(said)
            return
        if self.session.phase == "plan_review":
            if YES.match(said):
                await self.accept_plan()
            else:
                await self.revise_plan(said)
            return
        if (new := NEW_SUBJECT.match(said)) is not None:
            await self.new_goal(new.group("topic"))
            return
        if self.plans.active() is None:
            finished = self.plans.get(self.session.plan_id) if self.session.plan_id else None
            if finished is not None and finished.status == "completed":
                await self.run_turn(Trigger.LEARNER_MESSAGE, learner_text=said)  # e.g. answering a last question
            else:
                await self.create_plan(said)
            return
        if short and SHOW_PLAN.search(said):
            self.ui.note(self.plan_markdown())
            return
        lesson = self.lesson()
        stage = self.session.exercise_stage if lesson is not None else None
        if stage == "explain":
            if short and SKIP.match(said):
                await self.skip_prediction()
            elif said.endswith("?"):  # a question before predicting: answer it, keep the prediction open
                await self.run_turn(Trigger.EXPLAIN, learner_text=said)
            else:
                await self.answer_prediction(said)
            return
        if stage == "attempt" and lesson is not None:
            await self._during_attempt(lesson, said, short)
            return
        # Between steps: the current step has not started yet, or the last one just finished.
        if short and SKIP.match(said):
            await self.skip_step()
        elif short and NEXT.match(said):
            await self.start_step()
        else:  # nothing to check between steps, so request_check is not offered
            tools = [*(t for t in actions.TOOLS if t.name != "request_check"), actions.START_TOOL]
            calls = await self.run_turn(Trigger.LEARNER_MESSAGE, learner_text=said, tools=tools)
            if not await self._act_on(calls, None):
                await self._ensure_reply()

    async def _during_attempt(self, lesson: Lesson, said: str, short: bool) -> None:
        """The learner is doing the task. Saying they are done, answering, or asking for help all just work."""
        if short and DONE.match(said):
            await self.check()
            return
        if short and HINT.search(said):
            await self.hint()
            return
        if short and SOLUTION.search(said):
            await self.solution()
            return
        if short and SKIP.match(said):
            await self.skip_step()
            return
        if _has_answer_check(lesson) and short and not said.endswith("?"):
            result = await self.check(answer=said, auto=True)  # their words might be the answer itself
            if result is not None and result.passed:
                return
        tools = [*actions.TOOLS, *([actions.ASSESS_TOOL] if _needs_review(lesson) else [])]
        calls = await self.run_turn(Trigger.LEARNER_MESSAGE, learner_text=said, tools=tools)
        if not await self._act_on(calls, lesson):
            await self._ensure_reply()

    async def _act_on(self, calls: list[ToolCall], lesson: Lesson | None) -> bool:
        """Carry out what the tutor decided from the learner's plain-English message. True if it acted."""
        for parsed in (actions.validate(c) for c in calls):
            if isinstance(parsed, actions.RequestCheck) and lesson is not None:
                await self.check(answer=parsed.answer.strip() or None)
                return True
            if isinstance(parsed, actions.AssessStep) and lesson is not None and _needs_review(lesson):
                attempt = self.evidence.current_attempt(lesson.id) or self.evidence.start_attempt(
                    self.session_id, lesson, self.session.mode
                )
                self._apply_verdict(lesson, attempt.id, parsed)
                return True
            if isinstance(parsed, actions.StartStep) and lesson is None:
                await self.start_step()
                return True
        return False

    async def _ensure_reply(self) -> None:
        """The model wrote nothing and took no action: the learner must still get an answer."""
        if not self.last_reply.strip():
            await self.run_turn(
                Trigger.LEARNER_MESSAGE,
                tools=[],
                extra_trusted=["You did not reply yet. Answer the learner's last message in words."],
            )

    async def run_turn(
        self,
        trigger: Trigger,
        *,
        learner_text: str = "",
        verification: VerificationResult | None = None,
        hint_text: str | None = None,
        hint_level: int | None = None,
        wait: bool = True,
        tools: list[ToolSpec] | None = None,
        extra_trusted: list[str] | None = None,
        visible: bool = True,
        plan_budget: bool = False,
    ) -> list[ToolCall]:
        fl = self.inflight
        if fl is not None and not fl.task.done():
            if trigger in SOLICITED:
                await self.cancel_turn("superseded", "replaced by your newer request")
            else:
                return []  # never overlap an unsolicited response
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
            phase=self.session.phase,
            plan_id=self.session.plan_id,
            extra_trusted=extra_trusted or [],
        )
        settings = self.config.provider_settings(self.provider.name)  # type: ignore[arg-type]
        out_tokens = settings.plan_max_output_tokens if plan_budget else settings.max_output_tokens
        out_tokens = max(out_tokens, MIN_REPLY_TOKENS)
        ctx = self.assembler.build(
            inputs,
            max_context_tokens=max(settings.max_context_tokens, out_tokens + 4000),
            max_output_tokens=out_tokens,
        )
        if learner_text:
            self.sessions.add_message(self.session_id, "learner", learner_text, trigger=trigger.value)
        self.turn_counter += 1
        turn_id = self.turn_counter
        task = asyncio.create_task(
            self._turn(turn_id, trigger, inputs, ctx, tools=tools, out_tokens=out_tokens, visible=visible)
        )
        self.inflight = InFlight(turn_id, trigger, ctx.revision, task)
        # The task's own last status update still counts it as running; refresh once it has really ended.
        task.add_done_callback(lambda _t: self.push_status())
        self.push_status()
        if wait:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return self._turn_tools.pop(turn_id, [])

    def _meta(self, inputs: TurnInputs, ctx: AssembledContext) -> TurnMeta:
        lesson = inputs.lesson
        plan = self.plans.get(self.session.plan_id) if self.session.plan_id else None
        nxt = self.plans.current(plan) if plan is not None and plan.status == "active" else None
        last = self.state.last_command()
        error = None
        if last is not None and last.exit_status:
            # The pane the command ran in (the shell), not whichever pane comes first (often Neovim).
            pane = self.state.panes.get(last.pane or "")
            if pane is None or not (pane.tail or pane.snapshot):
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
                "phase": self.session.phase,
                "plan_title": plan.title if plan else "",
                "next_step": f"step {nxt.position}: {nxt.title}" if nxt else "",
                "step_position": nxt.position if nxt else 1,
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

    async def _turn(
        self,
        turn_id: int,
        trigger: Trigger,
        inputs: TurnInputs,
        ctx: AssembledContext,
        *,
        tools: list[ToolSpec] | None = None,
        out_tokens: int = 1024,
        visible: bool = True,
    ) -> None:
        label = MOCK_LABEL if not self.provider.makes_network_calls else f"{self.provider.name} · {self.provider.model}"
        meta = self._meta(inputs, ctx)
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        status = "complete"
        note = ""
        if visible:
            self.ui.tutor_begin(turn_id, trigger.value, label)
        try:
            if not await self._ensure_network():
                raise ProviderError("not_configured", "Sending to the provider was not approved.")
            settings = self.config.provider_settings(self.provider.name)  # type: ignore[arg-type]
            request = ProviderRequest(
                model=self.provider.model,
                system=ctx.system,
                messages=ctx.messages,
                max_output_tokens=out_tokens,
                tools=tools if tools is not None else actions.TOOLS,
                meta=meta,
            )
            async with asyncio.timeout(settings.timeout_seconds * (4 if tools else 2) + 5):
                async for event in self.provider.stream(request):
                    if isinstance(event, TextDelta):
                        text_parts.append(event.text)
                        if visible:
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
                    elif isinstance(event, Done) and event.stop_reason in CUT_OFF:
                        note = "This reply hit the length limit and was cut short. Ask “can you finish that?”"
            s = assess(trigger, ctx.revision, self.state)
            if trigger in SOLICITED and s.material and trigger not in NO_STALE_NOTE:
                status = "stale"
                note = f"Heads up: {s.reason}, so this answer may be out of date. Ask again for a fresh look."
        except asyncio.CancelledError:
            fl = self.inflight
            status = fl.cancel_status if fl and fl.turn_id == turn_id else "cancelled"
            note = fl.cancel_note if fl and fl.turn_id == turn_id else ""
            if visible:
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
            if visible:
                self.ui.tutor_end(turn_id, status, note)
            else:
                self.ui.note(note, "error")
            self.sessions.add_message(
                self.session_id,
                "note",
                f"provider error: {err.kind}",
                trigger=trigger.value,
                status="error",
            )
            if trigger in (
                Trigger.LESSON_INTRO,
                Trigger.PREDICTION,
                Trigger.HINT,
                Trigger.EXPLAIN,
                Trigger.VERIFICATION,
                Trigger.REPEATED_FAILURE,
            ):
                self.ui.note(
                    "**Lesson text (shown without the AI because the provider failed):**\n\n" + compose(meta),
                    "info",
                )
            self.push_status()
            return
        if visible:
            self.last_reply = "".join(text_parts)
            self.ui.tutor_end(turn_id, status, note)
            if text_parts:
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
        self._turn_tools[turn_id] = tool_calls
        for call in tool_calls:
            if call.name not in CALLER_HANDLED_TOOLS:
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
