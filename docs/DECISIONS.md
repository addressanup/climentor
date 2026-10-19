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
unless the learner asked with `/check` (no "failure" for work in progress).
Checks that run learner code never run automatically.

## 2026-10-19 — Shell announcement is best effort; sync recovers
A shell started before the tutor listens skips its "session started" message
without using a sequence number. After a real gap, shell status returns to
`current` once events flow again, while the "history incomplete" note stays.
