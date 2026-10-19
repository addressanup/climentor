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
| Offline demo (`mentor demo`) | Done | Real tools, mock tutor, separate data |
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
| Sync status (current / catching-up / disconnected / paused / incomplete) | Done | Words in status line and tmux status bar |
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

## Curriculum and verification

| Feature | Status | Notes |
|---|---|---|
| Versioned lesson format, data-only | Done | `docs/CURRICULUM.md` |
| 3 CLI, 3 Neovim, 3 Python exercises | Done | Each verifier tested with right and wrong work |
| Technique evidence for Neovim (mode events, opt-in counts) | Done | Otherwise reported "unverified" |
| Host execution of Python checks with limits | Done | Not a sandbox; permission required |
| Safe setup/cleanup of exercise files | Done | |
| Git, PostgreSQL, debugging, other languages, systems tracks | Planned | Extension routes documented |
| Strong sandboxing (containers/VMs, `sandbox-exec`) | Planned | |

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
