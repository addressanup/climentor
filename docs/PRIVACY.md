# Privacy, data, and deletion

## What is observed

Only while observation is on, and only inside the CLI Mentor tmux session:

| Source | What | What is skipped |
|---|---|---|
| Shell pane (zsh hooks) | Command text, start/end time, exit status, working directory | Commands starting with a space; anything typed into programs (REPLs, password prompts) |
| Shell pane screen (tmux) | Output stream and screen snapshots | The tutor's own pane |
| Neovim (plugin) | Buffers inside approved folders, including unsaved edits; mode; cursor (coalesced); saves; diagnostics | Out-of-scope paths, symlinks that escape, `.env`/keys/credentials and similar, `node_modules`/`.venv`/`.git`, binaries, files over 256 KB, terminal/help/unnamed buffers |
| Files | Read on demand inside approved folders | Same deny list as above |
| Optional command counts | Names like `dw: 2` in one exercise file | Everything else; raw keys are never recorded |

The shell pane is observed wherever you `cd` (command records), but file
*contents* are only read inside approved folders.

## Where data is stored

| What | Where | Kept for |
|---|---|---|
| Settings (no secrets) | `~/.config/cli-mentor/config.toml` | Until you delete it |
| Learning data (profile, evidence, sessions) | `~/.local/share/cli-mentor/mentor.sqlite3` (+ `-wal`, `-shm` files) | Until you delete it |
| Raw observations (redacted) | same database, `events` table | 7 days (configurable) |
| Conversation text | same database, `messages` table | 30 days (configurable) |
| Logs (metadata only, keys masked) | `~/.local/state/cli-mentor/logs/` | Rotated at 2 MB, 3 old files |
| Session token, socket, zsh wrapper | `$TMPDIR/cli-mentor-<uid>/<session>/` | Removed when the session ends |
| API keys | macOS Keychain, service `cli-mentor` | Until `mentor key delete <provider>` |

All folders are created private (0700) and files 0600. High-frequency raw
terminal chunks and cursor moves are kept in memory only, not written to disk.

## What is sent to the model provider

Only when a real provider is configured and you approved sending. Each request
contains the tutor policy, your confirmed profile, recent conversation, the
current exercise, labelled memory, and (if observation is on) a redacted view
of recent commands, the shell screen, and the focused Neovim buffer. `/context`
shows exactly this before you send anything. The mock provider sends nothing
anywhere.

**Data already sent to a provider cannot be recalled by CLI Mentor.** The
provider's own retention policy applies.

## Pause

`/observe pause`:
- closes the tmux observation connection and tells Neovim to stop sending;
- shell-hook messages that still arrive are discarded on arrival (not stored,
  not sent);
- queued events are dropped; unprompted tutor replies in progress are cancelled.

`/observe resume` asks first, then takes fresh snapshots. Nothing from the
pause is replayed, and the tutor is told that command history has a gap.

## Redaction

Best effort, never a guarantee. Patterns cover common API keys and tokens,
private key blocks, `PASSWORD=...`-style assignments, `--password` flags,
`Authorization:` headers, and credentials in URLs. Capturing less comes first;
redaction is the second line.

## Deleting data

| Goal | How |
|---|---|
| One memory note | `/forget <id>` or `mentor memory forget <id>` |
| This session's stored observations | `/forget observations` |
| A whole session | `mentor delete-session <id>` (add `--with-evidence` to remove its learning evidence too) |
| Everything | See `docs/UNINSTALL.md` |
| Revoke permissions | `/permissions revoke <capability>` |

After deleting, CLI Mentor folds the SQLite write-ahead log (WAL) back into
the main file and runs `VACUUM`, so deleted rows do not linger in free pages
or the WAL file. This is **not forensic secure erasure**: copies can remain in
backups (Time Machine), APFS snapshots, the SSD's internal storage, logs you
copied elsewhere, and anything already sent to a provider.
