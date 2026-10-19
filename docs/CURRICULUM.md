# Curriculum format and extensions

## Principles

- A lesson file is **data only** (TOML). It never contains commands to run.
- Each lesson names a **verifier id**. Verifiers are trusted Python classes in
  `src/cli_mentor/curriculum/adapters/`. A downloaded lesson cannot add code.
- The engine enforces the order: explain (words and symbols) → example →
  prediction → task → check → transfer question.
- A model's opinion is never a check result.

## Lesson format, version 1

File: `src/cli_mentor/curriculum/tracks/<track>/<NN>-<name>.toml`

| Field | Meaning |
|---|---|
| `format_version` | Always `1` for this format |
| `id`, `version` | e.g. `cli.pipelines`, `1`. Bump `version` when the task changes. |
| `title`, `track`, `order` | Display and ordering |
| `skills` | Skill tags this lesson gives evidence for (e.g. `cli.pipes.pipe`) |
| `prerequisites` | Lesson ids that should come first |
| `objectives` | What the learner will be able to do |
| `[[vocabulary]]` | `term` + `meaning`, in plain words |
| `[[syntax]]` | `example` + `parts` (`token`, `meaning`) + optional `note`: symbol-by-symbol reading |
| `demonstration` | Markdown example shown before the task |
| `prediction`, `prediction_answer` | Question asked before the task, and the expected answer |
| `instructions` | The task. Only shown after the explanation and prediction. |
| `workdir` | Relative folder inside the practice folder |
| `[[setup]]` | Files to create: `path`, `content` or a trusted `generator` + `generator_params` |
| `[[hints]]` | `level` 1–3, smallest first |
| `solution` | Shown only on `/solution` (recorded as full help) |
| `verifier`, `[verifier_params]` | Trusted checker id and its data parameters |
| `rubric` | What the checker looks for (shown to the model) |
| `transfer` | Questions that apply the idea somewhere new |
| `cleanup` | `keep` or `remove_created` |
| `risk` | `read_only`, `writes_workspace`, or `runs_learner_code` |
| `telemetry` | `none` or `nvim_normal_commands` (opt-in command counts) |

Setup paths must be relative and cannot contain `..`. Setup never overwrites
existing files (except `/lesson reset`, after asking). Cleanup only removes
files the setup created that the learner has not changed.

## Writing a verifier

```python
from typing import ClassVar
from ..verify import Check, VerificationResult, VerifyContext, register


@register
class MyVerifier:
    id: ClassVar[str] = "track.thing.v1"
    executes_learner_code: ClassVar[bool] = False  # True needs the "run learner code" permission

    async def verify(self, ctx: VerifyContext) -> VerificationResult:
        text = ctx.read("result.txt")  # scope-checked, redacted read
        ok = text is not None and text.strip() == "42"
        return VerificationResult(self.id, ok, [Check("result.txt says 42", ok, "...", "filesystem")])
```

Available evidence in `VerifyContext`: files (through the scope checker),
observed shell records since the attempt started, Neovim buffer mirrors, mode
history and opt-in command counts, the learner's typed answer, and (only with
permission) a `HostRunner` that runs Python with limits. Report technique as
`verified` only when there is evidence for *how* the work was done.

## Built-in tracks (implemented)

| Track | Exercises |
|---|---|
| CLI fundamentals | `cli.navigation`, `cli.files`, `cli.pipelines` |
| Neovim fundamentals | `nvim.modes`, `nvim.motions`, `nvim.operators` |
| Python fundamentals | `python.expressions`, `python.types`, `python.functions` |

## Extension routes (designed for, NOT implemented)

These are places the format and verifier interface are meant to grow. None of
them ships as a curriculum today.

| Area | How it would plug in | Notes |
|---|---|---|
| Git | Verifier reads the repo with `git` plumbing commands run by trusted code in the exercise folder (read-only: `git log --format`, `git status --porcelain`). | Needs a `run_checks` adapter that runs fixed git commands; never commands from lesson files. |
| PostgreSQL | A disposable database per exercise (e.g. `initdb` in the exercise folder) and verifiers that run fixed SQL queries. | Needs the `mutate_database` capability, which is deliberately disabled in this version. |
| Debugging and testing | Verifiers that run the learner's tests with `HostRunner` and inspect results; Neovim diagnostics as evidence. | Host execution, so keep it behind the permission. |
| Other languages | New runner per language (same limits pattern as `HostRunner`). | Tool availability checks belong in `mentor doctor`. |
| Systems, networking, security, infrastructure | Lessons that read observed command records and outputs; containers or VMs for anything that needs isolation. | Do not claim isolation unless a real sandbox is used. |
| Advanced projects | Project-mentor mode plus rubric-based reviews, profiling, concurrency, failure diagnosis, design trade-offs. | Needs richer evidence than pass/fail; out of scope for v1. |
