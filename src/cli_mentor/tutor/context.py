"""Build the model request from trust-separated sections.

Sections, in order of trust:
  system           trusted tutor policy + confirmed profile
  <guidance>       trusted, per trigger
  <learner_progress>  trusted: where the learner is in the program, what to suggest next
  <exercise>       trusted curriculum data
  <retrieved_memory>  stored memory, each line labelled with provenance
  <tool_results>   trusted checker output
  <observed_workspace trust="untrusted">  redacted, tag-neutralised
  <learner_message>

Everything observed is redacted (best effort) before it is placed here.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..curriculum.model import Lesson
from ..curriculum.plans import PlanStore
from ..curriculum.verify import VerificationResult
from ..events.schema import SourceKind
from ..memory.evidence import EvidenceStore
from ..memory.notes import Notes
from ..memory.profile import Profile
from ..memory.sessions import Sessions
from ..observe.privacy import is_password_prompt, redact_text
from ..observe.workspace import WorkspaceScope
from ..providers.base import ChatMessage, estimate_tokens
from ..state.projection import WorkspaceState
from .policy import Trigger
from .prompts import TRIGGER_GUIDANCE, program_overview, system_prompt
from .staleness import ContextRevision, relevant_keys

Trust = Literal["trusted", "memory", "tool", "untrusted", "learner"]
_TAG = re.compile(
    r"</?\s*(observed_workspace|mentor_context|learner_message|learner_progress|tool_results|retrieved_memory|exercise|guidance)\b",
    re.I,
)


def neutralise(text: str) -> str:
    """Stop untrusted text from closing or opening our section tags."""
    return _TAG.sub(lambda m: m.group(0).replace("<", "‹"), text)


def tilde(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home) :] if path.startswith(home) else path


@dataclass
class Section:
    name: str
    trust: Trust
    text: str


@dataclass
class Budget:
    commands: int = 8
    snapshot_lines: int = 30
    tail_chars: int = 1500
    buffer_radius: int = 40
    files: int = 40


@dataclass
class AssembledContext:
    system: str
    messages: list[ChatMessage]
    sections: list[Section]
    revision: ContextRevision
    observation_included: bool
    estimated_tokens: int
    trimmed: list[str] = field(default_factory=list)

    def render_for_display(self) -> str:
        parts = [f"Context revision {self.revision.revision} · ~{self.estimated_tokens} tokens (estimate)"]
        if self.trimmed:
            parts.append("Trimmed to fit budget: " + ", ".join(self.trimmed))
        parts.append(f"Observation included: {'yes' if self.observation_included else 'no'}")
        for s in self.sections:
            parts.append(f"--- {s.name} [{s.trust}] ---\n{s.text}")
        return "\n\n".join(parts)


@dataclass
class TurnInputs:
    trigger: Trigger
    mode: str
    session_id: str
    learner_text: str = ""
    lesson: Lesson | None = None
    stage: str | None = None
    hint_level: int = 0
    hint_text: str | None = None
    verification: VerificationResult | None = None
    observation_allowed: bool = True
    exercise_dir: Path | None = None
    goal: str | None = None
    phase: str = "learning"
    plan_id: str | None = None
    extra_trusted: list[str] = field(default_factory=list)


class ContextAssembler:
    def __init__(
        self,
        state: WorkspaceState,
        scope: WorkspaceScope,
        profile: Profile,
        notes: Notes,
        evidence: EvidenceStore,
        sessions: Sessions,
        clock: Callable[[], float] = time.time,
        plans: PlanStore | None = None,
    ) -> None:
        self.state = state
        self.scope = scope
        self.profile = profile
        self.notes = notes
        self.evidence = evidence
        self.sessions = sessions
        self.clock = clock
        self.plans = plans

    # ------------------------------------------------------------ sections
    def _progress(self, t: TurnInputs) -> str:
        sessions = len(self.sessions.list_sessions(500))
        lines = [
            "This is the learner's first CLI Mentor session."
            if sessions <= 1
            else f"They have used CLI Mentor before (session {sessions}).",
            f"Teaching mode: {t.mode}. Observation: {'on' if t.observation_allowed else 'off or paused'}.",
        ]
        if t.goal:
            lines.append(f"Session goal: {t.goal}")
        plan = self.plans.get(t.plan_id) if self.plans and t.plan_id else None
        if t.phase == "ask_goal" or plan is None:
            lines.append(
                "No accepted learning plan yet. The tutor asked what they want to learn; their answer starts a plan."
            )
        elif t.phase == "plan_review" or plan.status == "proposed":
            lines.append(
                f"A plan “{plan.title}” is proposed and waits for the learner to accept it (reply yes) "
                "or ask for changes."
            )
        else:
            assert self.plans is not None
            done, total = self.plans.progress(plan.id)
            lines.append(
                f"Learning plan: “{plan.title}” (their goal, in their words: “{plan.goal[:200]}”). "
                f"Progress: {done} of {total} steps done. Status: {plan.status}."
            )
            steps = self.plans.steps(plan.id)
            current = next((st for st in steps if st.step_id == plan.current_step), None)
            if current is not None:
                stage_next = {
                    "explain": "they should answer the prediction question in their own words (or say skip)",
                    "attempt": "they are doing the task; when they say (in any words) that they are done, or give "
                    "an answer, the work is checked (call request_check); they can say hint if stuck",
                    "verified": "finished; the next step starts when they say they are ready (call start_step)",
                }
                started = t.lesson is not None and t.lesson.order == current.position
                lines.append(
                    f"Current step {current.position} of {total} (module “{current.module}”): {current.title}: "
                    f"{current.objective}. "
                    + (
                        stage_next.get(t.stage or "", "")
                        if started
                        else "Not started yet: it starts when they say they are ready (call start_step)."
                    )
                )
                upcoming = [st.title for st in steps if st.position > current.position][:3]
                if upcoming:
                    lines.append("Coming next: " + "; ".join(upcoming))
            elif plan.status == "completed":
                lines.append(
                    "Every step is finished. Offer a short review, or a new subject (they say “new subject:” and a topic)."
                )
        due = self.evidence.due_reviews()
        if due:
            lines.append("Skills due for review (offer a short review): " + ", ".join(sk.skill for sk in due[:6]))
        return "\n".join(lines)

    def _exercise(self, t: TurnInputs) -> str:
        les = t.lesson
        if les is None:
            return "\n".join(["No exercise is active.", *t.extra_trusted])
        lines = [
            f"Exercise: {les.title} (id {les.id} v{les.version}), stage: {t.stage}",
            f"Track: {les.track}",
        ]
        if t.goal:
            lines.append(f"Session goal: {t.goal}")
        lines.append("Objectives: " + "; ".join(les.objectives))
        if t.trigger in (Trigger.LESSON_INTRO, Trigger.EXPLAIN):
            lines.append("Explanation material:\n" + les.explanation_markdown())
        if t.trigger == Trigger.LESSON_INTRO and les.prediction:
            lines.append(f"Prediction question to ask: {les.prediction}")
        if t.trigger == Trigger.EXPLAIN and t.stage == "explain" and les.prediction:
            lines.append(f"Prediction question still open (ask it again at the end): {les.prediction}")
        if t.trigger == Trigger.PREDICTION:
            lines.append(f"Prediction question: {les.prediction}\nExpected answer: {les.prediction_answer}")
        if t.stage not in ("explain", None) or t.trigger == Trigger.PREDICTION:
            lines.append("Task instructions:\n" + les.instructions.strip())
        if t.hint_text:
            lines.append(f"Hint to use (level {t.hint_level} of {les.max_hint_level}): {t.hint_text}")
        if t.trigger == Trigger.VERIFICATION and les.transfer:
            lines.append("Transfer questions you may ask: " + " | ".join(les.transfer))
        lines.extend(t.extra_trusted)
        return "\n".join(lines)

    def _memory(self, t: TurnInputs) -> str:
        lines: list[str] = []
        query = " ".join(filter(None, [t.learner_text, t.lesson.title if t.lesson else ""]))
        items = self.notes.search(query, limit=6) if query.strip() else []
        for item in self.notes.items(status="confirmed", limit=6):
            if item not in items:
                items.append(item)
        for item in items[:10]:
            label = "confirmed" if item.status == "confirmed" else "PROPOSED, not confirmed"
            lines.append(f"- [{item.provenance}, {label}] {item.text}")
        if t.lesson:
            states = {s.skill: s for s in self.evidence.skill_states()}
            for skill in t.lesson.skills:
                s = states.get(skill)
                if s:
                    lines.append(
                        f"- [observed evidence] {skill}: {s.state.replace('_', ' ')} "
                        f"({s.independent_passes} independent, {s.assisted_passes} assisted, {s.failures} failed checks)"
                    )
                else:
                    lines.append(f"- [no evidence yet] {skill}")
        return "\n".join(lines) if lines else "Nothing stored yet."

    def _observation(self, t: TurnInputs, b: Budget) -> str:
        st = self.state
        now = self.clock()
        sync = st.overall_sync()
        statuses = {k: st.source_status(k) for k in (SourceKind.TMUX, SourceKind.SHELL, SourceKind.NVIM)}
        per = ", ".join(f"{k.value}={v.value if v else 'not connected'}" for k, v in statuses.items())
        last_event = max((s.last_event_at for s in st.sources.values()), default=0.0)
        age = f"{now - last_event:.0f}s ago" if last_event else "no events yet"
        out = [f"Sync: overall={sync.value} ({per}); newest event {age}."]
        if st.shell_history_incomplete:
            out.append("Note: some shell events were missed; command history is incomplete.")
        if st.cwd:
            out.append(f"Shell working directory: {tilde(st.cwd)}")
        cmds = list(st.commands)[-b.commands :]
        if cmds:
            out.append("Recent commands (structured shell records, newest last):")
            for c in cmds:
                where = tilde(c.cwd)
                dur = f", {c.duration:.1f}s" if c.duration is not None else ""
                out.append(f"  [exit {c.exit_status}{dur}] in {where}: {redact_text(c.command)}")
        for c in st.running.values():
            out.append(f"Running now: {redact_text(c.command)}")
        shell_panes = [p for p in st.panes.values() if p.snapshot or p.tail]
        for pane in shell_panes[:2]:
            if pane.snapshot:
                lines = [ln for ln in pane.snapshot if not is_password_prompt(ln)][-b.snapshot_lines :]
                taken = time.strftime("%H:%M:%S", time.localtime(pane.snapshot_at or now))
                out.append(f"Screen of pane {pane.pane_id} (rendered snapshot taken {taken}; NOT exact history):")
                out += [f"  {redact_text(ln)}" for ln in lines]
            if pane.tail and b.tail_chars:
                tail = pane.tail[-b.tail_chars :]
                out.append(f"Output stream of pane {pane.pane_id} since then (approximate, colours removed):")
                out += [f"  {redact_text(ln)}" for ln in tail.splitlines()[-40:]]
        ed = st.editor
        if st.buffers or ed.mode:
            mode_names = {
                "n": "Normal",
                "i": "Insert",
                "v": "Visual",
                "V": "Visual line",
                "c": "Command-line",
                "R": "Replace",
            }
            out.append(f"Neovim mode: {mode_names.get(ed.mode[:1], ed.mode) or 'unknown'}")
            focused = st.focused_buffer()
            for buf in sorted(st.buffers.values(), key=lambda m: m is not focused):
                state = (
                    "UNSAVED changes (newer than the file on disk)"
                    if buf.modified
                    else "saved (matches disk when last written)"
                )
                out.append(
                    f"Buffer {tilde(buf.path)}: {len(buf.lines)} lines, {state}{' [focused]' if buf is focused else ''}"
                )
                if buf is focused or len(st.buffers) == 1:
                    cur_line = (
                        ed.cursor[2] if ed.cursor and (ed.cursor[0], ed.cursor[1]) == (buf.source_id, buf.buf) else 0
                    )
                    lo = max(0, cur_line - b.buffer_radius)
                    hi = min(len(buf.lines), cur_line + b.buffer_radius + 1)
                    if ed.cursor:
                        out.append(f"  cursor at line {cur_line + 1}, column {ed.cursor[3] + 1}")
                    window = redact_text("\n".join(buf.lines[lo:hi])).split("\n")
                    out += [f"  {lo + i + 1:4d}| {ln}" for i, ln in enumerate(window)]
                for d in buf.diagnostics[:10]:
                    out.append(f"  diagnostic line {d.line + 1}: {redact_text(d.message)}")
        if t.exercise_dir is not None and b.files:
            files = self.scope.list_files(t.exercise_dir, limit=b.files)
            if files:
                out.append(f"Files in exercise folder {tilde(str(t.exercise_dir))}: " + ", ".join(files))
        return neutralise("\n".join(out))

    # ------------------------------------------------------------ build
    def build(self, t: TurnInputs, *, max_context_tokens: int, max_output_tokens: int) -> AssembledContext:
        profile_lines = [f.value for f in self.profile.confirmed()]
        system = system_prompt(t.mode, profile_lines, program_overview())
        files = (
            [os.path.realpath(t.exercise_dir / s.path) for s in t.lesson.setup] if t.lesson and t.exercise_dir else []
        )
        keys = relevant_keys(files)
        revision = ContextRevision(
            revision=self.state.revision,
            keys=self.state.snapshot_revisions(keys),
            exercise_dir=os.path.realpath(t.exercise_dir) if t.exercise_dir else None,
        )
        history: list[ChatMessage] = []
        for role, content, status in self.sessions.recent_messages(t.session_id, limit=12):
            if role == "note" or status not in ("complete", "stale", "cut_off") or not content.strip():
                continue  # some APIs reject empty messages
            history.append(ChatMessage("user" if role == "learner" else "assistant", content))
        budget = Budget()
        trimmed: list[str] = []
        steps: list[tuple[str, Callable[[Budget], None]]] = [
            ("terminal stream", lambda bb: setattr(bb, "tail_chars", 0)),
            ("screen lines", lambda bb: setattr(bb, "snapshot_lines", 12)),
            ("buffer window", lambda bb: setattr(bb, "buffer_radius", 12)),
            ("file list", lambda bb: setattr(bb, "files", 0)),
            ("older commands", lambda bb: setattr(bb, "commands", 3)),
            (
                "older conversation",
                lambda bb: history.__delitem__(slice(0, max(0, len(history) - 4))),
            ),
        ]
        while True:
            sections = [
                Section("guidance", "trusted", TRIGGER_GUIDANCE.get(t.trigger.value, "")),
                Section("learner_progress", "trusted", self._progress(t)),
                Section("exercise", "trusted", self._exercise(t)),
                Section("retrieved_memory", "memory", self._memory(t)),
            ]
            if t.verification is not None:
                sections.append(Section("tool_results", "tool", t.verification.summary()))
            if t.observation_allowed:
                sections.append(Section("observed_workspace", "untrusted", self._observation(t, budget)))
            else:
                sections.append(
                    Section(
                        "observed_workspace",
                        "untrusted",
                        "Observation is paused or not permitted. You have NO current view of the workspace.",
                    )
                )
            body = self._render(t, revision, sections)
            total = estimate_tokens(system) + sum(estimate_tokens(m.content) for m in history) + estimate_tokens(body)
            if total + max_output_tokens <= max_context_tokens or not steps:
                break
            name, apply = steps.pop(0)
            apply(budget)
            trimmed.append(name)
        if t.learner_text:
            sections.append(Section("learner_message", "learner", t.learner_text))
        messages = [*history, ChatMessage("user", body)]
        return AssembledContext(system, messages, sections, revision, t.observation_allowed, total, trimmed)

    def _render(self, t: TurnInputs, revision: ContextRevision, sections: list[Section]) -> str:
        parts = [f'<mentor_context revision="{revision.revision}" trigger="{t.trigger.value}">']
        for s in sections:
            if s.name == "observed_workspace":
                parts.append(f'<observed_workspace trust="untrusted">\n{s.text}\n</observed_workspace>')
            else:
                parts.append(f"<{s.name}>\n{s.text}\n</{s.name}>")
        parts.append("</mentor_context>")
        if t.learner_text:
            parts.append(f"<learner_message>\n{t.learner_text}\n</learner_message>")
        return "\n".join(parts)
