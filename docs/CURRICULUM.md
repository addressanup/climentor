# Learning plans

There is no fixed curriculum. You say what you want to learn; the AI tutor
designs a plan; trusted code checks your work.

## Flow

1. **Welcome.** The first time (or when no plan is active) the tutor asks:
   "What would you like to learn?"
2. **Plan.** Your answer (for example "C++ from the very basics to the most
   advanced features") goes to the model, which must call the `propose_plan`
   tool: modules in order, each with small steps. The program shows it.
3. **Review.** Reply **yes** (or `/plan accept`) to start, or say what to
   change; the model revises it. Nothing is saved as your plan until you accept.
4. **Steps, one at a time.** When you reach a step, the model prepares it
   (`prepare_step`) using what the tutor knows about you so far. Then the
   usual order applies: new words and symbols explained → example →
   prediction → task → check → transfer question.
5. **Next sessions.** `mentor start` lists your subjects (progress, last
   studied, folder) and asks which one to continue, or `n` for a new one.
   The chosen subject reopens its own session and conversation, with the
   panes in its folder: "Welcome back. Next is step 7: …"

## Subjects and folders

Each accepted plan is a **subject** with its own folder in the practice
folder, for example `~/cli-mentor-workspace/c-from-the-very-basics/`, and
numbered step folders inside it (`01-hello-c/`, `02-variables-and-types/`).
The folder name is fixed when the plan is made. `mentor subjects` lists them;
`mentor start --subject 2` continues one without asking; `mentor start --new`
starts a new one.

Commands: `/learn <topic>`, `/plan`, `/plan accept`, `/plan skip`,
`/plan list`, `/plan switch <id>`, `/lesson`, `/check [answer]`.

## What the model may send

Both tools are validated with Pydantic before anything happens
(`src/cli_mentor/curriculum/plans.py`).

- **Plan outline:** title, goal summary, assumptions, 1–20 modules, 1–12 steps
  each, at most 150 steps.
- **Step details:** skills, vocabulary, symbol-by-symbol syntax, example,
  prediction (+ answer), instructions, up to 6 starter files (relative paths
  only, no `..`), 1–3 hints, up to 6 checks, solution, transfer questions.

Starter files are written into the practice folder only after you approve.

## Check menu (fixed; run by trusted code)

| Check | What trusted code does |
|---|---|
| `file_exists` | The file exists in the step folder |
| `file_contains` | The file contains a piece of text (plain text, no regex) |
| `command_succeeded` | A command starting with that program (e.g. `c++`, `make`) was observed finishing with exit 0 |
| `program_output` | Compiles C (`cc -std=c17`) or C++ (`c++ -std=c++20`), or runs Python, then compares the output. Flags are fixed in code. Needs your "run my code" approval. Host execution with limits, **not a sandbox**. |
| `learner_answer` | Compares your `/check <answer>` with accepted answers (ignores case and punctuation) |
| `editor_commands` | Opt-in Neovim command counts; only affects "technique verified/unverified" |

The model chooses which checks to use and their data. It cannot add commands,
compiler flags, or new check types. The checker id is `plan.generic.v1`
(`src/cli_mentor/curriculum/adapters/generic.py`).

**Steps without an automatic check** are reviewed by the tutor (`assess_step`
tool: complete / needs work). That review is recorded as **unverified** and
does not change skill states. `/plan skip` records a step as skipped.

## Offline (mock) tutor

The mock cannot design plans. It says a real model is needed. For tests and
demos it can replay a recorded plan: set `CLI_MENTOR_MOCK_FIXTURE` to a JSON
file like `tests/fixtures/cpp_plan.json`.

## Limits and ideas for later

- Plan quality depends on the model. Plans are a suggestion you can change.
- Only C, C++ and Python programs can be run by checks today. Other languages
  need a new runner in `curriculum/runner.py` (same limit pattern).
- Databases (PostgreSQL), Git internals, and infrastructure exercises need new
  trusted check types; `mutate_database` stays disabled until then.
