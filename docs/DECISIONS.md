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
