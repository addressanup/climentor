# Architecture

CLI Mentor is one Python process (the **tutor process**) running inside the left
tmux pane, plus two tiny helpers that live inside the learner's real tools:

- a zsh hook file (session-scoped, loaded through a private `ZDOTDIR`), and
- a Neovim Lua plugin (session-scoped, loaded through `--cmd "set rtp^=..."`).

```
tmux server "cli-mentor"  (dedicated: tmux -L cli-mentor)
└── session mentor-<id>
    ├── pane: tutor   → python -m cli_mentor tutor --session <id>
    │     ├── Textual UI (chat, status, approvals)
    │     └── Engine (asyncio)
    │          ├── IPC server  ← zsh hooks, Neovim plugin  (Unix socket + token)
    │          ├── tmux control-mode client (tmux -C attach, ignore-size)
    │          ├── privacy filter + scope check → Event envelopes
    │          ├── Sequencer (per-source seq, gaps, duplicates, epochs)
    │          ├── State projection (reducers, revisions, sync status)
    │          ├── Intervention policy (deterministic)
    │          ├── Context assembler (trust-labelled sections, budget)
    │          ├── Provider (mock | anthropic | openai)
    │          ├── Curriculum + trusted verifiers
    │          ├── Memory (SQLite)
    │          └── Permissions
    ├── pane: editor  → nvim (+ cli_mentor plugin)
    └── pane: shell   → zsh (+ session hooks)
```

## Data flow

```
authorized sources → validation + privacy filter → Event envelope
  → Sequencer → State projection (revision N) → Intervention policy
  → Context assembly (references revision N) → Provider stream
  → staleness check against current revisions → UI
```

The local process observes continuously while observation is on. The model is
called only at checkpoints chosen by the deterministic policy (learner request,
repeated failure, exercise result), never per keystroke.

## Modules (src/cli_mentor)

| Module | Job |
| --- | --- |
| `cli.py` | `mentor` command-line entry point (argparse). |
| `paths.py` | XDG directories, private runtime directory, permission checks. |
| `config.py` | Validated TOML configuration (no secrets). |
| `apikeys.py` | API keys from macOS Keychain (`keyring`) or environment. |
| `chatgpt_auth.py` | Sign in with ChatGPT (OpenAI's flow for open-source apps): browser sign-in with PKCE and a one-shot `127.0.0.1` return page, ID-token signature check, Keychain storage, locked token renewal, sign-out. |
| `doctor.py` | Dependency checks with fix instructions. Installs nothing. |
| `lifecycle.py` | Create/attach/stop the dedicated tmux session; cleanup of owned resources only. |
| `permissions.py` | Capabilities, scoped grants, deterministic decisions. |
| `db/` | SQLite connection, WAL, numbered migrations. |
| `events/` | Versioned event envelope, payload types, sequencer. |
| `observe/` | IPC server, tmux control-mode client, zsh integration, workspace scope, privacy filter. |
| `state/` | Reducers that project events into `WorkspaceState`. |
| `nvim/` | The Lua plugin shipped as package data. |
| `tutor/` | Engine (orchestrator), policy, context, prompts, staleness, model actions. |
| `providers/` | Provider interface, mock, Anthropic, OpenAI, and the ChatGPT plan (Responses API). |
| `curriculum/` | Lesson schema, loader, trusted verifiers per track. |
| `memory/` | Profile, evidence, skill states, review schedule, export, retention. |
| `ui/` | Textual tutor pane (`app.py`), full-screen setup (`setup.py`), shared header widgets (`widgets.py`), terminal-colour theme (`theme.py`). |

## Synchronisation contract

- Every event is an `EventEnvelope` (schema version 1): `event_id`, `session_id`,
  `source {kind, id, epoch}`, per-source `seq`, `captured_at` (source clock),
  `received_at` and `ingest_index` (local receipt order), `type`, `payload`,
  `provenance`.
- `ingest_index` is receipt order inside the tutor process. It is **not** a
  claim about true global order across processes.
- Gaps (`seq` jumps) mark a source `incomplete` and trigger a snapshot where a
  snapshot is possible (Neovim buffers, tmux `capture-pane`). Shell history
  cannot be recovered and stays marked incomplete for that range.
- Neovim `changedtick` must increase per buffer; older updates are dropped.
- A new `epoch` (source restarted) resets that source and requires a snapshot.
- Sync status per source: `current`, `catching-up`, `disconnected`, `paused`,
  `incomplete`. Always shown as words.
- Every tutor request records a `ContextRevision` (global revision plus
  per-key revisions). Before showing advice or running an approved action the
  engine checks whether relevant keys changed in a material way.

## Process and lifecycle model

- There is no separate daemon. The tutor process is the "daemon". If it
  crashes, the pane stays (remain-on-exit) and `mentor resume` respawns it.
  Shell hooks connect per event, and the Neovim plugin reconnects with backoff,
  so both recover automatically. The session token lives in a 0600 file in the
  0700 runtime directory so a restarted tutor can accept the same clients.
- `mentor start` never touches the learner's default tmux server.
- Quitting the tutor offers *detach*, *end session*, or *cancel*. Ending refuses
  to close panes that hold unsaved Neovim buffers unless the learner confirms.

## Trust boundaries in prompts

1. Trusted tutor policy (system prompt, written by us).
2. Confirmed learner profile (user-stated, confirmed).
3. Learner message.
4. Retrieved memory (labelled with provenance).
5. Untrusted observed workspace content (`<observed_workspace trust="untrusted">`,
   closing tags neutralised).
6. Tool results (from trusted verifiers, labelled).

Model output can only *propose* actions (`propose_memory`, `request_check`).
The engine validates and checks permissions. A model saying "you passed" is
never stored as evidence.
