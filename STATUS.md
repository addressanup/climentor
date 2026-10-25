# Status and handoff

Last updated: 2026-10-25

Public repository: https://github.com/addressanup/climentor (`origin`, default branch `main`).
The first GitHub CI run has started; its result is not yet verified:
https://github.com/addressanup/climentor/actions/runs/40162492577

## Conversational tutoring change

Scope: replace answer-key routing and visible assessment reports with short, continuous tutoring.
Interpret ordinary replies semantically, preserve a question across clarification or missing input,
continue after demonstrated understanding, and recover from input sent to the shell. Keep trusted
execution checks and existing permissions; distinguish task success from independent understanding.
This is a local, reversible change to the tutor flow (Tier 2). Verified with isolated regression tests,
the non-live suite, lint, and types. No paid model calls or real learner data were used.

Nineteen new conversation regression cases cover paraphrases, clarification without punctuation,
missing input, smaller corrective questions, restart persistence, pane mix-ups, mixed concept/file
checks, transfer evidence, saved-answer retries, unrelated terminal updates, and eager UI task completion.
The new semantic flow has not been tested with a live model.

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
  recovery, end-to-end tests, CI file, docs — done (first GitHub CI run in progress).

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

Command: `uv run pytest -m "not live"` → **242 passed, 2 deselected** (2026-10-25,
after the conversational tutoring changes).

| Group | Count | What they use |
|---|---|---|
| `tests/unit` | 223 | Pure logic, real SQLite, real IPC socket, real Python subprocesses, Textual Pilot (tutor pane and full-screen setup), recorded semantic replies, real SDK parsing via `httpx2.MockTransport`, ChatGPT sign-in against a fake OpenAI server with a real local return page |
| `tests/integration` | 17 | Real tmux (private sockets), real zsh with the hooks, real headless Neovim with the plugin |
| `tests/e2e` | 2 | The real `mentor` command, tmux, zsh, Neovim, and the Textual tutor pane, mock provider |
| `tests/live` | 2 | **Not run** (skipped: needs your API key and model id) |

Also run: `uv run ruff check .` (clean), `uv run ruff format --check .` (clean),
`uv run mypy` (strict, clean, 65 source files).

Current UI checks: the Textual Pilot suite, including conversational input hints, hidden teaching
stages, streaming/cancellation, and setup; plus the real private tmux end-to-end sessions.

Historical UI checks (before this change): screenshots of every tutor-pane state (first-run tour,
permission dialog, plan, file approval, lesson) at 76 and 60 columns, dark and
light colours, and of every setup page; plus a real isolated session in tmux
(pane labels, status bar text, tutor pane captured with colours at 75×49).

Historical manual checks (before this change): `mentor init --yes`, `mentor doctor`, `mentor start
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
- The tmux status bar was always blank: tmux 3.7 rejects `set-option -t =name`; now `=name:`.
  An end-to-end test checks the bar.
- Status-bar updates less than 0.5 s apart were dropped for good; the last one is now sent a moment later.
- While a question dialog was open, status updates looked for widgets in the dialog and failed.
- In the setup screens, Esc in the "change the words" box also went back a page, and list keys
  swallowed Enter; both fixed and covered by Pilot tests.

## UI, setup and first walkthrough (current)
- Tutor pane: terminal colours (light/dark guess, `CLI_MENTOR_THEME` override); header with subject,
  "step N of M", and a line that fills as steps get done; teaching stages stay internal; one-line status in words;
  input hint that says what to type next; labelled replies ("Mentor · hint · deepseek-chat"); question
  dialogs with letters, arrows or mouse.
- `mentor init`: full-screen, five steps, shared save logic with `--plain`/`--yes`; offers to start.
- First `mentor start` opens setup if needed; the first session begins with a short tour (`/tour` replays it).
- tmux: bars in terminal colours, pane labels "Mentor", "Editor · nvim", "Shell · zsh".

## Conversation in plain English (current)
- One idea, example, and question at a time; short feedback and automatic continuation after understanding.
- Conceptual replies use `interpret_reply`, accepting synonyms and paraphrases. Clarification requests
  preserve the question, including requests without a question mark. Corrections ask a smaller question.
- Trusted execution checks stay in the background. Their success alone never establishes understanding;
  semantic explanation/transfer evidence remains model-inferred and unverified, with help recorded.
- Pending questions survive restarts. Empty/scrambled replies and shell mix-ups produce no negative
  skill evidence; a shell mix-up gets a one-line reminder to use the left pane.
- A malformed interpretation is retried once with the saved reply. If processing still fails, “try again”
  reuses that saved reply. Textual's eager task scheduling is handled without losing the interpretation.
- Running the program successfully in the step folder checks the step by itself, if running code was
  allowed for the session.
- This new semantic flow is tested offline with recorded judgments and the mock, not a live model.
  The DeepSeek results below describe the earlier flow.
- Slash commands still work as optional shortcuts; no message suggests them any more.

## Real-model end-to-end test (DeepSeek `deepseek-flash`, 2026-10-20)
Two layers, in throwaway folders (the learner's real data untouched): the tutor engine driven turn by
turn, then a full real session (private tmux, the learner's own zsh and Neovim, the Textual tutor pane)
driven by keystrokes. 41 model calls, about 254k input and 57k output tokens.

Worked first time: a sensible 65–67 step C++ plan in ~21 s; step preparation; vocabulary and
symbol-by-symbol intros; prediction feedback; real compile errors quoted in hints; checks that ran the
learner's code; welcome back; plain-English "next", "skip", "ready for the next one".

Found and fixed (each has a regression test in `tests/unit/test_real_model_regressions.py`):
- The tutor pane did not scroll to new messages (a long plan stayed out of view); PageUp/PageDown were
  swallowed by the text box. Now the log is anchored to the newest message and the keys always scroll.
- Replies were cut at 1024 tokens (intros lost their prediction question); now at least 3000, and a cut
  reply says so.
- Right answers in a sentence failed the answer check; now a clear statement passes (not hedges or "not …").
- The model used the exact-answer check for "explain in your own words"; now only short facts are allowed,
  and a rejected step is retried once with the exact problem.
- Between steps, a tool-only reply left the learner with silence (and an empty bubble); now the model is
  asked for words, and empty bubbles are hidden.
- A question before predicting was recorded as the prediction; now it is answered and the prediction stays open.
- Steps without starter files had no folder; the learner was not told which folder to work in.
- "Yes, for this session" to running code covered one step only, so automatic checks never fired later.
- The "writing" spinner stayed on after replies; Neovim showed "incomplete"/"disconnected" forever after a
  tutor restart or after closing a second Neovim; hints could quote the Neovim pane instead of the shell.
- Cosmetic: "Module 1: Module 1: …", a literal "\n" in step text, a double full stop, folder names cut mid-word.

Still not tested with a real model: plan revision, the no-check AI review, hints at levels 2–3, later
modules, other languages, the ChatGPT plan.

## Broken replies and no sign of progress (2026-10-21)
Reported by the learner: replies stopped part way, and nothing showed whether the tutor was still
working, so a half-written reply looked finished. Found and fixed:
- `deepseek-flash` thinks before it answers, and the thinking used the reply's limit. The learner's
  stored replies ended mid-sentence ("### Two questions for") marked "complete". A direct DeepSeek
  call confirmed it: 169 thinking tokens, then the answer stopped at the limit (`finish_reason: length`).
  Thinking models now get 8000 tokens of room on top of the reply.
- The pane re-drew the whole reply on every update, and the draws queued. Measured on a stored
  6000-character reply streamed at model speed: about 50 draws behind, still writing 6 s after the
  model finished. Now it adds only new text, one draw at a time: never more than 9 characters behind,
  done within 0.15 s, and the same blocks as drawing the whole reply at once. A CSS rule that
  re-styled the whole reply on each draw was removed (before: 10000 characters took 66 s against an
  ideal 38 s; after: 8000 characters took 35 s against an ideal 30 s).
- A live "Thinking… 12s · Esc to stop" / "Writing…" line under the conversation, also while a step is
  being prepared; the input box says "Mentor is still replying…". A reply that still runs out of room
  says "! cut short" and stays in the history, so "please finish that" works.
- The reply timeout restarts with each streamed event, so a long reply that is still arriving never
  times out.
- Checked live with DeepSeek in a private tmux session (throwaway folders): a 5-step SQL plan, step 1
  prepared (23 s of "Thinking…"), its intro (2508 characters) and two more replies (3008 and 1216
  characters), all complete; "Thinking…" then "Writing…" then gone, each time. Regression tests:
  `test_thinking_models_get_room_on_top_of_the_reply`, `test_ui_streams_a_long_reply_without_falling_behind`,
  `test_ui_says_thinking_then_writing_until_the_reply_is_complete`, and the cut-off test.
- Seen during that run, not fixed yet: a request like "Before I predict: explain …" (no question mark)
  was taken as the prediction, so the step moved on while the tutor asked for the prediction again.

## ChatGPT plan (current)
- "Continue with ChatGPT" in `mentor init`, plus `mentor login` / `mentor logout [--forget]`. Uses OpenAI's
  Sign in with ChatGPT for open-source apps (same plan usage as Codex), not Codex's own login.
- Sign-in, ID-token signature check, Keychain storage, locked token renewal, sign-out and the Responses
  API provider are tested offline (fake OpenAI server, real local return page).
- Checked against the real service without signing in: OpenAI's sign-in settings and signing keys load;
  the sign-in link is accepted and leads to the ChatGPT log-in page; the token endpoint answers bad codes
  with `invalid_grant`, which we handle.
- NOT yet tested: a real sign-in, the model list, and a real tutor reply with tools.
- Also fixed: a new setup pre-filled the model box with the offline demo's model name.

## Learning flow (current)
Welcome asks what to learn → the model proposes a plan → learner accepts or revises → steps are prepared
on demand and checked by the fixed check menu (real C/C++ compile-and-run supported) → later sessions offer
to continue. Each subject has its own folder and session; `mentor start` asks which to continue.
Built-in lessons were removed by request. Tested with recorded plan answers (mock fixture) and, for
the first three steps of a C++ plan, with DeepSeek (see the real-model test above).

## Known limits / not done
- Live provider calls: DeepSeek (OpenAI-compatible `base_url`) tested end to end; Anthropic, OpenAI itself
  and the ChatGPT plan not yet (no key or sign-in here). SDK code paths are tested offline.
- Linux: not verified; CI Linux job is marked experimental.
- GitHub CI has started; a successful hosted run has not yet been verified.
- zsh only; REPL input is not observable.
- Learner code runs on the host (limits, not a sandbox).
- Git/PostgreSQL/debugging/other-language tracks: extension routes only.
- Logs rotate by size, not by age.

## Next steps (suggested order)
1. Run a live test with your key:
   `CLI_MENTOR_LIVE_ANTHROPIC_KEY=... CLI_MENTOR_LIVE_ANTHROPIC_MODEL=<id> uv run pytest -m live`
2. Try a real session: `uv run mentor init` (choose your provider), then `uv run mentor start`.
3. Check the first GitHub CI run.
4. Expand conversational transfer reviews, add a Git track, and age-based log cleanup.
