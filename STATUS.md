# Status and handoff

Last updated: 2026-10-19

## Where things stand

All four phases are implemented and tested on this Mac (macOS 27.0.1, Apple
Silicon). What is done and what is planned: `docs/FEATURES.md`.

- Phase 0: repo, lock file, design docs — done.
- Phase 1: vertical slice (CLI, doctor, tmux workspace, tutor UI, observation,
  CLI exercise, mock + real providers, persistence, pause/resume) — done.
- Phase 2: Neovim plugin (unsaved buffers, mode/cursor, resync, Neovim
  exercises) — done.
- Phase 3: profile, evidence, skill states, review schedule, resume,
  3 exercises per track — done.
- Phase 4: permissions, privacy/deletion, backpressure, stale-advice handling,
  recovery, end-to-end tests, CI file, docs — done (CI not yet run on GitHub).

## Environment used
- macOS 27.0.1, Apple Silicon (arm64)
- Python 3.12.13 (Homebrew) via uv 0.12.2
- tmux 3.7c, Neovim 0.11.5, zsh 5.9 (zsh/net/socket available), SQLite 3.53 with FTS5
- No model API key present.

## Locked dependency versions (uv.lock, `exclude-newer = 2026-10-12`)
anthropic 1.13.0, openai 3.29.0, httpx2 2.13.1, textual 8.2.8, pydantic 2.14.1,
keyring 25.7.0, tomlkit 0.15.1, rich 15.0.0; dev: pytest 9.1.1,
pytest-asyncio 1.4.0, mypy 2.4.0, ruff 0.17.0, pynvim 0.6.0.

## Tests actually run (latest)

Command: `uv run pytest -m "not live"` → **157 passed, 2 deselected** (run 3
times in a row with the same result after the last fix).

| Group | Count | What they use |
|---|---|---|
| `tests/unit` | 138 | Pure logic, real SQLite, real IPC socket, real Python subprocesses, Textual Pilot, real SDK parsing via `httpx2.MockTransport` |
| `tests/integration` | 17 | Real tmux (private sockets), real zsh with the hooks, real headless Neovim with the plugin |
| `tests/e2e` | 2 | The real `mentor` command, tmux, zsh, Neovim, and the Textual tutor pane, mock provider |
| `tests/live` | 2 | **Not run** (skipped: needs your API key and model id) |

Also run: `uv run ruff check .` (clean), `uv run ruff format --check .` (clean),
`uv run mypy` (strict, clean, 66 source files).

Manual checks run: `mentor init --yes`, `mentor doctor`, `mentor start
--detach`, a full lesson typed into the panes (approval dialog, explanation,
prediction, two deliberate mistakes, small hint quoting the real error, fix,
`/check`, evidence with assistance level 1), quit via Ctrl+Q → end (no
processes left), and `mentor demo --detach`. All in temp folders with private
tmux sockets.

### Acceptance criteria → evidence

| # | Criterion | Covered by |
|---|---|---|
| 1–2 | Start with documented commands; tutor + real workspace appear | e2e `test_acceptance_learning_flow` |
| 3 | Syntax explained before the task | e2e + `test_lesson_explains_before_assigning` |
| 4–6 | Deliberate mistake, actual error received, small hint | e2e + `test_repeated_failure_gives_small_hint_with_actual_error` |
| 7–9 | Correction, trusted verifier, evidence shows assistance | e2e + `test_full_exercise_with_hint_records_assistance` |
| 10 | Restart and resume with memory | e2e `test_restart_resume_and_provider_failure` + `test_resume_session_keeps_memory` |
| 11 | Pause keeps new observation from the provider | e2e + `test_pause_blocks_new_observation_from_provider` |
| 12 | Unsaved Neovim changes in tutor context | e2e (events) + `test_unsaved_editor_changes_reach_context` (provider request) + nvim integration |
| 13 | Stale advice suppressed | `test_stale_hint_is_withdrawn_when_learner_fixes_it`, `test_solicited_answer_marked_stale` (engine level, not through the UI) |
| 14 | Provider failure keeps the workspace | e2e (missing key → mock fallback) + `test_provider_failure_is_clear_and_falls_back` |
| 15 | No orphaned processes on shutdown | e2e (`pgrep` after ending the session) |

## Bugs found and fixed during testing
- tmux 3.7 prints `%pause` inside the reply block → parser fix.
- Neovim `:edit!` dropped buffers (async detach) → synchronous detach.
- Opt-in counter ignored operator-pending mode → mode check fix.
- Textual Markdown wiped fast replies on mount → `LiveMarkdown`.
- `MentorApp._log` overrode a Textual internal → renamed.
- Startup "incomplete" shell status from a harmless lost announcement → fixed.
- Two flaky tests (wait conditions matched too early) → fixed.
- Real-model greeting was generic (asked intake questions, invented its own exercise): the system prompt now
  describes the program (panes, commands, curriculum) and each turn carries a trusted `<learner_progress>`
  section (first session, finished exercises, current stage, suggested next exercise, due reviews).
- DeepSeek via `--base-url`: errors and the init prompt now name the server contacted.

## Known limits / not done
- Live provider calls not tested (no key here). SDK code paths are tested offline.
- OpenAI-compatible `base_url` servers: unverified.
- Linux: not verified; CI Linux job is marked experimental.
- CI workflow written but never run (no git remote).
- zsh only; REPL input is not observable.
- Learner code runs on the host (limits, not a sandbox).
- Git/PostgreSQL/debugging/other-language tracks: extension routes only.
- Logs rotate by size, not by age.

## Next steps (suggested order)
1. Run a live test with your key:
   `CLI_MENTOR_LIVE_ANTHROPIC_KEY=... CLI_MENTOR_LIVE_ANTHROPIC_MODEL=<id> uv run pytest -m live`
2. Try a real session: `uv run mentor init` (choose your provider), then `uv run mentor start`.
3. Push to GitHub and check the CI run.
4. Add a quiz bank for `/review`, a Git track, and age-based log cleanup.
