# Implemented vs planned

Status keys: **Done** = implemented and covered by tests that were run ·
**Partial** = works with stated limits · **Planned** = not implemented.
Test evidence is listed in `STATUS.md`.

## Workspace and lifecycle

| Feature | Status | Notes |
|---|---|---|
| `mentor start` creates a dedicated tmux session (tutor, Neovim, zsh) | Done | Private server `tmux -L cli-mentor`; e2e tested |
| Attach / detach / resume / stop | Done | Resume respawns a crashed tutor or rebuilds panes |
| Launch inside existing tmux | Done | Prints instructions, or `--nested` |
| Partial-startup rollback | Done | Kills only the session it just created |
| Quit protects unsaved work | Done | Warns about unsaved Neovim buffers and running commands |
| Stale socket / runtime cleanup | Done | Only our own folders, only if nothing is listening |
| No orphaned processes on shutdown | Done | e2e test checks with `pgrep` |
| `mentor doctor` | Done | Installs nothing |
| Full-screen setup (`mentor init`), plain fallback (`--plain`, `--yes`) | Done | Both save through one function; Pilot tests drive the full-screen path; offers to start the first session |
| First `mentor start` opens setup when needed | Done | Only at an interactive terminal |
| Subject picker with progress bar and "studied 2 days ago" | Done | |
| tmux bars in your terminal's colours, pane labels (Mentor / Editor · nvim / Shell · zsh) | Done | Status bar shows subject, step, observation, model |
| Offline demo (`mentor demo`) | Partial | Real tools and mock tutor; cannot design plans |
| Linux support | Planned | Code avoids macOS-only calls where practical; CI job is experimental; not verified |
| bash / fish shell hooks | Planned | zsh only |

## Observation and synchronisation

| Feature | Status | Notes |
|---|---|---|
| tmux control mode: parsing, octal decoding, pane attribution | Done | Real tmux tests |
| Tutor pane excluded | Done | Discarded by pane id |
| Snapshots for baseline, gaps, reconnect, resume | Done | Labelled "rendered screen, not history" |
| Flow control (`%pause` → continue + snapshot) | Done | tmux emits `%pause` inside the reply block; handled |
| Reconnect with backoff; pane close/new pane | Done | |
| zsh command records (text, cwd, exit, timing) | Done | Session-scoped `ZDOTDIR`; your dotfiles load unchanged |
| Neovim plugin: snapshots + incremental lines + changedtick | Done | Random-edit test incl. undo/redo |
| Unsaved vs on-disk distinction | Done | |
| Reload, detach, editor restart, resync | Done | |
| Mode, cursor (coalesced), saves, diagnostics | Done | Diagnostics forwarded when an LSP/linter produces them (no LSP is configured by us) |
| Sensitive / out-of-scope / terminal buffers skipped | Done | Checked in the plugin and again in the engine |
| Versioned envelopes, per-source seq, gap/duplicate detection, epochs | Done | |
| Bounded queues and backpressure | Done | Drop + mark incomplete + resync |
| Sync status (current / catching-up / disconnected / paused / incomplete) | Done | Problems are named in words in the status line and tmux status bar ("Neovim disconnected"); `/observe` shows every source |
| Pause / resume semantics | Done | See `docs/PRIVACY.md` |
| REPL input observation | Planned | Hooks cannot see input inside REPLs (stated to the model) |
| Exact per-command output attribution | Partial | Output stream and command records come from different channels; labelled approximate |

## Tutor

| Feature | Status | Notes |
|---|---|---|
| Six teaching modes | Done | Mode text in the system policy; deterministic rules per mode |
| Explain → demonstrate → predict → task → check → transfer | Done | Enforced by the engine stage machine |
| Graduated hints with assistance levels 0–4 | Done | `/hint`, `/solution` |
| Deterministic intervention policy (debounce, cooldown, thresholds, no overlap) | Done | |
| Stale advice: unsolicited withdrawn, solicited annotated | Done | Engine tests with timed streams |
| Trust-separated prompt sections, tag neutralisation | Done | Prompt-injection test |
| Validated model actions (`propose_memory`, `request_check`) | Done | Model inferences stay "proposed" |
| Curriculum-only fallback when the provider fails | Done | |
| Streaming, multiline input, cancel (Esc), approvals, readable code | Done | Textual app |
| Tutor pane header: subject, step N of M, learn → try → check, a line that fills as steps get done | Done | Words and shapes (● ○ ✓), not only colour |
| Input hint says what to type next (goal, yes/changes, prediction, "done") | Done | |
| Continuous assessment in plain English (no commands needed) | Done (offline-tested) | "done", answers, "hint", "next", "skip" handled directly; longer messages: the model calls `request_check` / `assess_step` / `start_step`. Real-model tool use not yet tested |
| Automatic check after you run your program successfully | Done | Only if you allowed running your code for the session |
| Uses the terminal's own colours (light or dark) | Done | Guessed from `COLORFGBG`; `CLI_MENTOR_THEME` overrides |
| First-session tour (screen, keys, how we work), `/tour` to replay | Done | Not sent to the model |
| Question dialogs: letter keys, or ↑ ↓ and Enter, or click | Done | |
| Rich review sessions (spaced quizzes generated per skill) | Partial | `/review` lists due skills and asks the model for a question; no dedicated quiz bank |
| Model-judged transfer answers as evidence | Planned | Answers are stored as unverified evidence only |

## Providers

| Feature | Status | Notes |
|---|---|---|
| Mock provider (deterministic, offline, labelled) | Done | |
| Anthropic (official SDK, streaming, tools) | Done (offline-tested) | SDK paths tested with recorded streams; live test needs your key |
| OpenAI (official SDK, Chat Completions, tools) | Done (offline-tested) | Same; `base_url` for compatible servers is unverified |
| Timeouts, bounded retries, rate-limit messages, cancellation | Done | |
| Usage accounting; estimated vs actual labelled | Done | Cost only from your configured prices |
| Keys in Keychain or env; never in config/logs/exports/prompts | Done | |

## Learning plans and verification

| Feature | Status | Notes |
|---|---|---|
| Welcome asks "What would you like to learn?" | Done | Deterministic first message, stored for context |
| AI-designed linear plan (propose, revise, accept) | Done (offline-tested) | Validated tool call; tested with recorded answers; quality depends on your model |
| Steps prepared on demand using memory and evidence | Done (offline-tested) | `prepare_step` tool, validated, turned into a normal lesson |
| Later sessions offer to continue | Done | "Welcome back… next is step N" |
| Fixed check menu run by trusted code | Done | files, commands, C/C++/Python output, answers, opt-in editor counts |
| Real C/C++ compile-and-run checks | Done | Apple clang; fixed flags; host execution with limits, not a sandbox |
| AI review for steps without automatic checks | Done | Recorded as unverified |
| Built-in fixed lessons | Removed | By request; the mock tutor cannot teach without a recorded plan |
| Other languages' program checks (Rust, Go, Java…) | Planned | Needs a runner per language |
| Database / Git / infrastructure check types | Planned | |
| Strong sandboxing (containers/VMs) | Planned | |

## Memory, privacy, permissions

| Feature | Status | Notes |
|---|---|---|
| Profile with provenance, onboarding confirm/edit/reject | Done | |
| Skill evidence, states, review schedule (heuristic) | Done | No mastery percentages |
| Session state and resume | Done | e2e: crash → resume → memory intact |
| Memory notes: FTS search, confirm/reject/correct/forget | Done | |
| Export, delete-session, retention purge, WAL checkpoint + VACUUM | Done | Not forensic erasure |
| Scoped, revocable permissions; never-grantable capabilities | Done | |
| Authenticated local IPC (0700/0600, peer uid, token) | Done | Same-user malware is out of scope |
| Retention UI for logs | Partial | Logs rotate by size; no age-based purge |
