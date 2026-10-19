"""AI-designed learning plans.

1. The learner says what they want to learn.
2. The model proposes a linear plan (modules -> steps) with the ``propose_plan``
   tool. It stays "proposed" until the learner accepts it.
3. Each step is prepared only when the learner reaches it (``prepare_step``),
   so it can use what the tutor has learned about them so far. The details are
   validated and turned into a normal ``Lesson``.
4. Checks come from a FIXED menu run by trusted code (``plan.generic.v1``). The
   model only chooses which checks to use and their data. It cannot add
   commands; compilers and flags are chosen by trusted code.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from ..db.store import Store
from ..providers.base import ToolSpec
from .model import Hint, Lesson, SetupFile, SyntaxBreakdown, SyntaxPart, Vocab

MAX_STEPS = 150
EDITOR_COMMANDS = frozenset(
    {
        "w",
        "b",
        "e",
        "W",
        "B",
        "E",
        "0",
        "$",
        "^",
        "G",
        "gg",
        "n",
        "N",
        "{",
        "}",
        "/",
        "?",
        "f",
        "t",
        "F",
        "T",
        "h",
        "j",
        "k",
        "l",
        "dd",
        "cc",
        "yy",
        "dw",
        "cw",
        "yw",
        "de",
        "ce",
        "d$",
        "c$",
        "diw",
        "ciw",
        "daw",
        "caw",
        'ci"',
        'di"',
        "ci(",
        "di(",
        "ci{",
        "di{",
    }
)


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _relative(value: str) -> str:
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts or not p.parts or value.startswith("~"):
        raise ValueError(f"path must be relative and stay inside the step folder: {value}")
    return value


# ---------------------------------------------------------------- outline
class StepOutline(_M):
    title: str = Field(min_length=3, max_length=120)
    objective: str = Field(min_length=3, max_length=400)
    new_concepts: list[str] = Field(default_factory=list, max_length=10)


class ModuleOutline(_M):
    title: str = Field(min_length=3, max_length=120)
    summary: str = Field(default="", max_length=600)
    steps: list[StepOutline] = Field(min_length=1, max_length=12)

    @field_validator("title")
    @classmethod
    def _unnumbered(cls, value: str) -> str:
        # Models often write “Module 3: Loops”; the plan adds the number itself.
        return re.sub(r"^\s*(module|part|unit)\s+\d+\s*[:.)\-–—]\s*", "", value, flags=re.I) or value


class PlanOutline(_M):
    title: str = Field(min_length=3, max_length=100)
    goal_summary: str = Field(default="", max_length=600)
    assumptions: list[str] = Field(default_factory=list, max_length=6)
    modules: list[ModuleOutline] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def _limit(self) -> PlanOutline:
        if sum(len(m.steps) for m in self.modules) > MAX_STEPS:
            raise ValueError(f"a plan may have at most {MAX_STEPS} steps")
        return self


# ---------------------------------------------------------------- check menu
class FileExists(_M):
    type: Literal["file_exists"]
    path: str = Field(max_length=200)

    @field_validator("path")
    @classmethod
    def _inside(cls, value: str) -> str:
        return _relative(value)


class FileContains(_M):
    type: Literal["file_contains"]
    path: str = Field(max_length=200)
    text: str = Field(min_length=1, max_length=300)

    @field_validator("path")
    @classmethod
    def _inside(cls, value: str) -> str:
        return _relative(value)


class CommandSucceeded(_M):
    """A command whose first word is ``program`` was observed finishing with exit 0."""

    type: Literal["command_succeeded"]
    program: str = Field(pattern=r"^[A-Za-z0-9_.+-]{1,40}$")


class ProgramOutput(_M):
    """Compile (trusted flags) and run one source file; compare its output."""

    type: Literal["program_output"]
    language: Literal["cpp", "c", "python"]
    source: str = Field(max_length=200)
    stdin: str = Field(default="", max_length=10_000)
    expected_stdout: str = Field(max_length=20_000)

    @field_validator("source")
    @classmethod
    def _inside(cls, value: str) -> str:
        return _relative(value)


class EditorCommands(_M):
    """Opt-in: these Normal-mode commands were used in the step's file (technique only)."""

    type: Literal["editor_commands"]
    commands: list[str] = Field(min_length=1, max_length=6)

    @field_validator("commands")
    @classmethod
    def _known(cls, value: list[str]) -> list[str]:
        unknown = [c for c in value if c not in EDITOR_COMMANDS]
        if unknown:
            raise ValueError(f"unsupported editor commands: {unknown}")
        return value


class LearnerAnswer(_M):
    """A short factual answer (a name, a number, a type), compared without case or punctuation.

    Not for “explain in your own words” questions: wording varies, so a word match would fail
    correct explanations. The tutor discusses those instead.
    """

    type: Literal["learner_answer"]
    question: str = Field(min_length=3, max_length=400)
    accepted: list[str] = Field(
        min_length=1,
        max_length=10,
        description="Short factual answers only, at most 4 words each, e.g. ['int', 'an int', 'integer'].",
    )

    @field_validator("accepted")
    @classmethod
    def _short(cls, value: list[str]) -> list[str]:
        long = [a for a in value if len(a.split()) > 4]
        if long:
            raise ValueError(
                "learner_answer is for short facts (at most 4 words per accepted answer); "
                "do not use it for explanations in the learner's own words"
            )
        return value


Check = Annotated[
    FileExists | FileContains | CommandSucceeded | ProgramOutput | EditorCommands | LearnerAnswer,
    Field(discriminator="type"),
]
CHECKS_ADAPTER: TypeAdapter[list[Check]] = TypeAdapter(list[Check])


def describe_check(c: Check) -> str:
    if isinstance(c, FileExists):
        return f"file {c.path} exists"
    if isinstance(c, FileContains):
        return f"{c.path} contains “{c.text}”"
    if isinstance(c, CommandSucceeded):
        return f"a `{c.program}` command finished without errors"
    if isinstance(c, ProgramOutput):
        return f"{c.source} compiles/runs and prints the expected output"
    if isinstance(c, EditorCommands):
        return "used " + ", ".join(c.commands) + " in Neovim (only if you allow command counting)"
    return f"answer to: {c.question}"


# ---------------------------------------------------------------- step details
class Concept(_M):
    term: str = Field(min_length=1, max_length=80)
    meaning: str = Field(min_length=1, max_length=500)


class SyntaxPartGen(_M):
    token: str = Field(min_length=1, max_length=80)
    meaning: str = Field(min_length=1, max_length=300)


class SyntaxGen(_M):
    example: str = Field(min_length=1, max_length=200)
    parts: list[SyntaxPartGen] = Field(min_length=1, max_length=15)
    note: str = Field(default="", max_length=500)


class StarterFile(_M):
    path: str = Field(max_length=200)
    content: str = Field(default="", max_length=20_000)

    @field_validator("path")
    @classmethod
    def _inside(cls, value: str) -> str:
        return _relative(value)


class StepDetails(_M):
    skills: list[str] = Field(min_length=1, max_length=5)
    vocabulary: list[Concept] = Field(default_factory=list, max_length=12)
    syntax: list[SyntaxGen] = Field(default_factory=list, max_length=6)
    demonstration: str = Field(default="", max_length=6000)
    prediction: str = Field(default="", max_length=600)
    prediction_answer: str = Field(default="", max_length=600)
    instructions: str = Field(min_length=10, max_length=4000)
    starter_files: list[StarterFile] = Field(default_factory=list, max_length=6)
    hints: list[str] = Field(min_length=1, max_length=3)
    checks: list[Check] = Field(default_factory=list, max_length=6)
    solution: str = Field(default="", max_length=6000)
    transfer: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("skills")
    @classmethod
    def _skill_names(cls, value: list[str]) -> list[str]:
        out = []
        for v in value:
            name = v.strip().lower().replace("c++", "cpp").replace("c#", "csharp")
            out.append(re.sub(r"_+", "_", re.sub(r"[^a-z0-9_.-]", "_", name)).strip("_")[:60])
        return [v for v in out if v] or ["general"]


# ---------------------------------------------------------------- tool schemas
def _inline(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve ``$ref``s so every provider gets one self-contained JSON schema."""
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                return walk(dict(defs[ref.split("/")[-1]]))
            return {k: walk(v) for k, v in node.items() if k != "discriminator"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    result: dict[str, Any] = walk(schema)
    return result


PLAN_TOOL = ToolSpec(
    "propose_plan",
    "Propose a linear learning plan for the learner's goal: modules in order, each with small steps. "
    "Start from the true basics unless the confirmed profile or evidence shows otherwise.",
    _inline(PlanOutline.model_json_schema()),
)
STEP_TOOL = ToolSpec(
    "prepare_step",
    "Prepare the current plan step as a hands-on exercise: vocabulary and symbol-by-symbol syntax, a short "
    "example, a prediction question, task instructions, starter files, up to 3 hints (smallest first), and "
    "checks chosen ONLY from the allowed check types.",
    _inline(StepDetails.model_json_schema()),
)


# ---------------------------------------------------------------- storage
@dataclass(frozen=True)
class StepInfo:
    plan_id: str
    step_id: str
    position: int
    module: str
    title: str
    objective: str
    concepts: list[str]
    status: str
    lesson: Lesson | None


@dataclass(frozen=True)
class PlanInfo:
    id: str
    goal: str
    title: str
    status: str
    outline: PlanOutline
    current_step: str | None
    created_at: float
    folder: str = ""
    session_id: str | None = None
    last_used: float | None = None

    @property
    def folder_name(self) -> str:
        return self.folder or f"{slug(self.title)}-{self.id[:6]}"


def slug(text: str, limit: int = 28) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(s) > limit:  # cut between words, not inside one
        cut = s[: limit + 1]
        s = cut[: cut.rfind("-")] if "-" in cut else s[:limit]
    return s.rstrip("-") or "plan"


def _prose(text: str) -> str:
    """Some models double-escape newlines in tool arguments, so the text shows a literal backslash-n.

    Only for prose: in code, a backslash-n can be a real escape such as ``"Hi\\n"``.
    """
    return text.replace("\\n", "\n") if "\\n" in text and "\n" not in text else text


def lesson_id(plan_id: str, step_id: str) -> str:
    return f"plan.p{plan_id[:8]}.{step_id}"


class PlanStore:
    def __init__(self, store: Store) -> None:
        self.store = store

    def _plan(self, row: Any) -> PlanInfo:
        return PlanInfo(
            row["id"],
            row["goal"],
            row["title"],
            row["status"],
            PlanOutline.model_validate_json(row["outline_json"]),
            row["current_step"],
            row["created_at"],
            row["folder"] or "",
            row["session_id"],
            row["last_used"],
        )

    def _step(self, row: Any) -> StepInfo:
        lesson = Lesson.model_validate_json(row["lesson_json"]) if row["lesson_json"] else None
        return StepInfo(
            row["plan_id"],
            row["step_id"],
            row["position"],
            row["module"],
            row["title"],
            row["objective"],
            json.loads(row["concepts"]),
            row["status"],
            lesson,
        )

    def _insert_steps(self, conn: Any, plan_id: str, outline: PlanOutline) -> None:
        position = 0
        for module in outline.modules:
            for step in module.steps:
                position += 1
                conn.execute(
                    "INSERT INTO plan_steps(plan_id, step_id, position, module, title, objective, concepts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        plan_id,
                        f"s{position}",
                        position,
                        module.title,
                        step.title,
                        step.objective,
                        json.dumps(step.new_concepts),
                    ),
                )

    def _unique_folder(self, title: str) -> str:
        base = slug(title, 40)
        taken = {r[0] for r in self.store.conn.execute("SELECT folder FROM learning_plans WHERE folder IS NOT NULL")}
        name, n = base, 2
        while name in taken:
            name, n = f"{base}-{n}", n + 1
        return name

    def create(
        self, goal: str, outline: PlanOutline, provider: str = "", model: str = "", session_id: str | None = None
    ) -> PlanInfo:
        plan_id = uuid.uuid4().hex
        now = time.time()
        folder = self._unique_folder(outline.title)
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO learning_plans(id, goal, title, status, outline_json, provider, model, created_at, "
                "updated_at, folder, session_id, last_used) VALUES (?, ?, ?, 'proposed', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    plan_id,
                    goal.strip()[:2000],
                    outline.title,
                    outline.model_dump_json(),
                    provider,
                    model,
                    now,
                    now,
                    folder,
                    session_id,
                    now,
                ),
            )
            self._insert_steps(conn, plan_id, outline)
        plan = self.get(plan_id)
        assert plan is not None
        return plan

    def revise(self, plan_id: str, outline: PlanOutline) -> PlanInfo:
        """Replace the outline of a plan that is still proposed."""
        with self.store.tx() as conn:
            conn.execute("DELETE FROM plan_steps WHERE plan_id = ?", (plan_id,))
            conn.execute(
                "UPDATE learning_plans SET title = ?, outline_json = ?, updated_at = ? WHERE id = ?",
                (outline.title, outline.model_dump_json(), time.time(), plan_id),
            )
            self._insert_steps(conn, plan_id, outline)
        plan = self.get(plan_id)
        assert plan is not None
        return plan

    def activate(self, plan_id: str) -> PlanInfo:
        first = self.store.conn.execute(
            "SELECT step_id FROM plan_steps WHERE plan_id = ? AND status IN ('pending', 'in_progress') "
            "ORDER BY position LIMIT 1",
            (plan_id,),
        ).fetchone()
        with self.store.tx() as conn:
            conn.execute(
                "UPDATE learning_plans SET status = 'archived', updated_at = ? WHERE status = 'active' AND id != ?",
                (time.time(), plan_id),
            )
            conn.execute(
                "UPDATE learning_plans SET status = 'active', current_step = COALESCE(current_step, ?), "
                "updated_at = ? WHERE id = ?",
                (first[0] if first else None, time.time(), plan_id),
            )
        plan = self.get(plan_id)
        assert plan is not None
        return plan

    def get(self, plan_id: str) -> PlanInfo | None:
        row = self.store.conn.execute("SELECT * FROM learning_plans WHERE id = ?", (plan_id,)).fetchone()
        return self._plan(row) if row else None

    def find(self, prefix: str) -> PlanInfo | None:
        rows = self.store.conn.execute("SELECT id FROM learning_plans WHERE id LIKE ?", (prefix + "%",)).fetchall()
        return self.get(rows[0][0]) if len(rows) == 1 else None

    def active(self) -> PlanInfo | None:
        row = self.store.conn.execute(
            "SELECT * FROM learning_plans WHERE status = 'active' ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        return self._plan(row) if row else None

    def touch(self, plan_id: str, session_id: str) -> None:
        """Remember which session studies this subject, and when it was last used."""
        self.store.conn.execute(
            "UPDATE learning_plans SET session_id = ?, last_used = ? WHERE id = ?", (session_id, time.time(), plan_id)
        )

    def subjects(self) -> list[PlanInfo]:
        """Accepted plans (subjects), most recently used first."""
        rows = self.store.conn.execute(
            "SELECT * FROM learning_plans WHERE status != 'proposed' ORDER BY COALESCE(last_used, updated_at) DESC"
        ).fetchall()
        return [self._plan(r) for r in rows]

    def list_plans(self) -> list[PlanInfo]:
        rows = self.store.conn.execute("SELECT * FROM learning_plans ORDER BY created_at DESC").fetchall()
        return [self._plan(r) for r in rows]

    def steps(self, plan_id: str) -> list[StepInfo]:
        rows = self.store.conn.execute(
            "SELECT * FROM plan_steps WHERE plan_id = ? ORDER BY position", (plan_id,)
        ).fetchall()
        return [self._step(r) for r in rows]

    def step(self, plan_id: str, step_id: str) -> StepInfo | None:
        row = self.store.conn.execute(
            "SELECT * FROM plan_steps WHERE plan_id = ? AND step_id = ?", (plan_id, step_id)
        ).fetchone()
        return self._step(row) if row else None

    def current(self, plan: PlanInfo) -> StepInfo | None:
        return self.step(plan.id, plan.current_step) if plan.current_step else None

    def save_lesson(self, plan_id: str, step_id: str, lesson: Lesson) -> None:
        self.store.conn.execute(
            "UPDATE plan_steps SET lesson_json = ?, status = CASE status WHEN 'pending' THEN 'in_progress' "
            "ELSE status END WHERE plan_id = ? AND step_id = ?",
            (lesson.model_dump_json(), plan_id, step_id),
        )

    def lesson(self, lid: str) -> Lesson | None:
        match = re.fullmatch(r"plan\.p([0-9a-f]{8})\.(s\d+)", lid)
        if not match:
            return None
        row = self.store.conn.execute(
            "SELECT lesson_json FROM plan_steps WHERE plan_id LIKE ? AND step_id = ?",
            (match.group(1) + "%", match.group(2)),
        ).fetchone()
        return Lesson.model_validate_json(row[0]) if row and row[0] else None

    def finish_step(self, plan_id: str, step_id: str, status: str = "done") -> StepInfo | None:
        """Mark a step done or skipped and move to the next one. Returns the next step, if any."""
        with self.store.tx() as conn:
            conn.execute(
                "UPDATE plan_steps SET status = ?, completed_at = ? WHERE plan_id = ? AND step_id = ?",
                (status, time.time(), plan_id, step_id),
            )
            nxt = conn.execute(
                "SELECT step_id FROM plan_steps WHERE plan_id = ? AND status IN ('pending', 'in_progress') "
                "ORDER BY position LIMIT 1",
                (plan_id,),
            ).fetchone()
            conn.execute(
                "UPDATE learning_plans SET current_step = ?, status = CASE WHEN ? IS NULL THEN 'completed' "
                "ELSE status END, updated_at = ? WHERE id = ?",
                (nxt[0] if nxt else None, nxt[0] if nxt else None, time.time(), plan_id),
            )
        return self.step(plan_id, nxt[0]) if nxt else None

    def progress(self, plan_id: str) -> tuple[int, int]:
        row = self.store.conn.execute(
            "SELECT SUM(status IN ('done', 'skipped')), COUNT(*) FROM plan_steps WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        return int(row[0] or 0), int(row[1] or 0)


# ---------------------------------------------------------------- conversion and display
def build_lesson(plan: PlanInfo, step: StepInfo, details: StepDetails) -> Lesson:
    """Turn validated, model-made step details into a normal Lesson (trusted code decides risk)."""
    runs_code = any(isinstance(c, ProgramOutput) for c in details.checks)
    uses_editor = any(isinstance(c, EditorCommands) for c in details.checks)
    return Lesson(
        id=lesson_id(plan.id, step.step_id),
        version=1,
        title=step.title,
        track="plan",
        order=step.position,
        skills=details.skills,
        objectives=[step.objective],
        vocabulary=[Vocab(term=c.term, meaning=_prose(c.meaning)) for c in details.vocabulary],
        syntax=[
            SyntaxBreakdown(
                example=s.example,
                note=_prose(s.note),
                parts=[SyntaxPart(token=p.token, meaning=p.meaning) for p in s.parts],
            )
            for s in details.syntax
        ],
        demonstration=_prose(details.demonstration),
        prediction=_prose(details.prediction),
        prediction_answer=_prose(details.prediction_answer),
        instructions=_prose(details.instructions),
        workdir=f"{plan.folder_name}/{step.position:02d}-{slug(step.title, 24)}",
        setup=[SetupFile(path=f.path, content=f.content) for f in details.starter_files],
        hints=[Hint(level=i + 1, text=_prose(text)) for i, text in enumerate(details.hints)],
        solution=details.solution,
        verifier="plan.generic.v1",
        verifier_params={"checks": [c.model_dump() for c in details.checks]},
        rubric=[describe_check(c) for c in details.checks] or ["reviewed by the tutor (no automatic check)"],
        transfer=[_prose(t) for t in details.transfer],
        risk="runs_learner_code" if runs_code else "writes_workspace",
        telemetry="nvim_normal_commands" if uses_editor else "none",
    )


def render_plan(outline: PlanOutline, steps: list[StepInfo] | None = None, current: str | None = None) -> str:
    status = {s.step_id: s.status for s in steps or []}
    lines = [f"## {outline.title}"]
    if outline.goal_summary:
        lines.append(outline.goal_summary)
    if outline.assumptions:
        lines.append("**Assumptions:** " + "; ".join(outline.assumptions))
    n = 0
    for mi, module in enumerate(outline.modules, start=1):
        lines.append(f"\n**Module {mi}: {module.title}**" + (f" — {module.summary}" if module.summary else "") + "\n")
        for step in module.steps:
            n += 1
            sid = f"s{n}"
            title = f"**{step.title}**" if sid == current else step.title
            tail = {"done": " ✓ done", "skipped": " (skipped)"}.get(
                status.get(sid, ""), " ← you are here" if sid == current else ""
            )
            lines.append(f"{n}. {title} — {step.objective}{tail}")
    return "\n".join(lines)
