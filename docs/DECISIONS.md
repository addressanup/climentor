# Decisions log

Each entry: date, decision, why, consequences.

## 2026-10-19 — Providers: Anthropic and OpenAI, bring-your-own-key
Learner choice. Both use official SDKs behind one interface. OpenAI uses Chat
Completions for wider compatibility with OpenAI-compatible servers (`base_url`).
Model IDs come from configuration (chosen from the provider's `models.list()`
during `mentor init`), never hard-coded.

## 2026-10-19 — Keys in macOS Keychain, env var fallback
Learner choice. Uses the `keyring` library (macOS backend talks to the Security
framework directly, so the key never appears in a process argument list).
Trade-off: any process running as the learner, after login, can ask Keychain for
the item; macOS may show an access prompt. Env vars are simpler but are
inherited by child processes, so the tutor reads the env var and the shell
pane never receives it from us.

## 2026-10-19 — One process, no separate daemon
The tutor process hosts both UI and engine. Simpler to study and to test (the
engine is a plain class used directly by tests). Restart is handled by
`remain-on-exit` + `mentor resume`, and both observation helpers reconnect.

## 2026-10-19 — Dedicated tmux server (`tmux -L cli-mentor`)
We never list, attach to, or kill anything on the learner's default tmux server.
Cleanup is limited to sessions we created on our own server.

## 2026-10-19 — Tutor pane excluded by discarding its output, not `-A off`
tmux docs: when *all* clients turn a pane off, tmux stops reading it. If the
learner detaches, our control client would be the only client and the tutor
pane could block on write, freezing the tutor. Discarding by pane ID is safe.

## 2026-10-19 — Pause closes the control-mode connection
Closing the control client stops tmux from sending us anything. Turning panes
off would risk the blocking behaviour above.

## 2026-10-19 — XDG directories on macOS
Same layout as Neovim on macOS (`~/.config`, `~/.local/share`, `~/.local/state`),
easy for a learner to find and edit. Runtime sockets go in
`$TMPDIR/cli-mentor-<uid>` (per-user, short enough for the 104-byte macOS
socket path limit).

## 2026-10-19 — argparse for the CLI
Standard library, no extra dependency, easy to read.

## 2026-10-19 — No file-watcher library
Files are read on demand at checkpoints (command finished, buffer saved,
verification) with scope checks. Fewer moving parts; good enough for exercises.

## 2026-10-19 — Supply-chain date pin
`[tool.uv] exclude-newer` keeps the lock to packages published at least seven
days earlier. Move the date forward on purpose when upgrading.

## 2026-10-19 — Model actions limited to two tools
`propose_memory` and `request_check`. Both validated with Pydantic and gated by
permissions. Assistant tool calls are stored internally and replayed to the
model as text notes, which avoids provider-specific tool-result pairing rules.

## 2026-10-19 — Shell hooks connect per event
zsh `zsocket` opens a short connection for each event. No persistent state in
the shell, so a restarted tutor is picked up automatically. Lost events show as
sequence gaps.

## 2026-10-19 — Session token passed as a file path, not a value
Values given to `tmux -e` appear in process arguments, which other local users
can list on macOS. Panes receive `CLI_MENTOR_TOKEN_FILE` (a 0600 file in the
0700 runtime folder) instead. The editor and shell panes are started through
`env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY`, so provider keys from the
environment never reach learner programs through us.

## 2026-10-19 — tmux reports `%pause`/`%continue` inside the reply block
Observed with tmux 3.7: after `refresh-client -A %N:pause` the `%pause`
notification is printed between that command's `%begin` and `%end`, although
the manual says notifications never appear inside blocks. The client extracts
these lines from `refresh-client` replies only. Output during a pause is lost,
so recovery always takes a `gap` snapshot.

## 2026-10-19 — Neovim `on_detach` handled synchronously
`:edit!` detaches and re-reads a buffer. A scheduled "detached" message
arrived after the fresh snapshot and dropped the buffer. Sending it inside the
callback keeps the order right (found by the reload integration test).

## 2026-10-19 — Telemetry parser accepts operator-pending mode
After `d`/`c`/`y`, Neovim reports mode `no`, not `n`. The opt-in counter treats
any mode starting with `n` as Normal mode.

## 2026-10-19 — Textual Markdown mount race
`Markdown` re-applies its constructor text when it mounts, which wiped fast
replies that finished first. `LiveMarkdown` also sets Textual 8.x's
`_initial_markdown` (a private attribute; the version is pinned in `uv.lock`).

## 2026-10-19 — High-frequency events stay in memory
`tmux.output` chunks and `nvim.cursor` moves are not written to SQLite. They
are low value for history and high volume; snapshots and structured records
are stored (redacted) instead.

## 2026-10-19 — Silent auto-checks
After a relevant command finishes or an exercise file is saved, non-executing
checkers run quietly. A pass is announced and recorded; a fail is not recorded
unless the learner asked for a check (no "failure" for work in progress).
Checks that run learner code run automatically only after a successful command
in the step folder, and only if the learner allowed running their code for the
session (updated by the continuous-assessment decision below).

## 2026-10-19 — Shell announcement is best effort; sync recovers
A shell started before the tutor listens skips its "session started" message
without using a sequence number. After a real gap, shell status returns to
`current` once events flow again, while the "history incomplete" note stays.

## 2026-10-19 — Learner-chosen goals and AI-designed plans replace the fixed curriculum
Learner request. The welcome asks what to learn; the model proposes a linear
plan (`propose_plan`), the learner accepts or revises it, and each step is
prepared when reached (`prepare_step`) so it can use memory and evidence.
Built-in lessons were removed completely (learner choice), so the offline mock
cannot teach unless given a recorded plan (`CLI_MENTOR_MOCK_FIXTURE`).

## 2026-10-19 — Plan steps are checked from a fixed menu
"Real checks + AI review" (learner choice). The model picks checks and data
from six types; trusted code runs them. Compiler and flags are fixed in code
(`c++ -std=c++20`, `cc -std=c17`). Steps without automatic checks get an AI
review recorded as unverified; skill states only change from trusted checks.
Risk (whether code runs) is derived by trusted code from the checks, not taken
from the model.

## 2026-10-19 — Plans are proposed until accepted
A model-made plan is stored as `proposed` and becomes `active` only after the
learner replies yes. One active plan at a time; others are archived and can be
switched back with `/plan switch`.

## 2026-10-19 — One folder and one session per subject
Learner request. Each accepted plan (subject) gets a folder named from its
title (unique, fixed at creation) and remembers the session that studies it.
`mentor start` asks which subject to continue; the chosen one reopens its own
session and conversation with the panes in its folder. A new session starts a
new subject. `/plan switch` inside a session still works but shares that
session's conversation.

## 2026-10-19 — The UI uses the terminal's own colours
Learner choice. The tutor pane, setup screens and tmux bars use ANSI colours,
so they match Neovim and the shell. Only the accent depends on light or dark
(guessed from `COLORFGBG`, overridden by `CLI_MENTOR_THEME`). Meaning is never
carried by colour alone: status and stages use words and shapes (● ○ ✓ ‖).

## 2026-10-19 — Full-screen setup, plain fallback, one save path
Learner choice. `mentor init` opens a Textual setup at a terminal. Scripts,
`--yes` and `--plain` use plain questions. Both build a `SetupAnswers` and save
it with `apply_answers`, so they cannot drift apart. In setup, Enter always
means "continue" (lists and checkboxes use Space), so the keys are the same on
every page. A pasted API key goes to the Keychain at once (needed to fetch the
model list); everything else is saved only at the review step.

## 2026-10-19 — First-session tour, not sent to the model
The very first session (no other sessions, no plans, nothing said yet) opens
with a short walkthrough of the screen before the permission question. It is
shown, not stored as a tutor message, so it does not use model context.
`/tour` replays it.

## 2026-10-19 — Continuous assessment in plain English
Learner request: no `/check` (or any command) should be needed. Short messages
are matched by the engine ("done", "hint", "next", "skip", "show the plan",
"new subject: …"), so this works even without a real model. Longer messages go
to the model with tools: `request_check` (with the learner's answer in their
own words), `assess_step` (steps without automatic checks) and `start_step`.
The model only decides *when* to check; trusted code still decides *whether*
the work passed, and model verdicts stay "unverified". A short message that
might be the answer is tried quietly first; a wrong guess records no failure.
Slash commands remain as optional shortcuts and are never suggested.

## 2026-10-19 — ChatGPT plan through Sign in with ChatGPT, not Codex's login
Learner request: use OpenAI models through the Codex (ChatGPT) subscription.
OpenAI documents a flow for open-source, locally run apps
(developers.openai.com/siwc/token-sharing-open-source): the first sign-in
registers the app (`client_id=dynamic_agent_client`, `agent_name_hint`, a
stable random `ext_agent_host_id`), then requests go to the public Responses
API with the user's access token. We use that instead of copying Codex's own
login (its client id, `~/.codex/auth.json`, or `chatgpt.com/backend-api`):
OpenAI says not to use those endpoints for this, and sharing Codex's rotating
refresh token would sign Codex out.
- Tokens are one JSON item in the Keychain (service `cli-mentor`, account
  `chatgpt`), like API keys, not the docs' example file.
- The ID token's signature is verified (PyJWT with `cryptography`, added for
  this), as the docs require.
- Requests follow the preview limits: `store: false`, `stream: true`,
  `instructions` for the system prompt, function tools grouped in one
  namespace, no `max_output_tokens`/`temperature`, no retries on 429.
- One ChatGPT account at a time. `mentor logout --forget` drops the
  registration so another account can sign in.
- The tutor status shows "using ChatGPT plan", and usage errors link to
  chatgpt.com/settings/usage, following OpenAI's UI guidelines.

## 2026-10-20 — Lessons from the first real-model run (DeepSeek)
- Every tutor reply gets at least 3000 output tokens; a reply that still hits the limit is labelled.
- `learner_answer` is for short facts (at most 4 words per accepted answer). A sentence that clearly
  states an accepted answer passes; hedges ("x or y") and negations ("not x") do not.
- A model plan or step that fails validation is retried once, with the field paths and reasons (never
  the values) sent back to the model.
- If the model answers with only a tool call that does nothing, the engine asks once more for words.
- "Run my code for this session" is granted for the subject's folder, so it covers every step.
- The conversation log uses Textual's anchoring: it follows new text, lets go when you scroll up, and
  follows again when you scroll to the end or send a message.
