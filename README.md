# CLI Mentor

A tutor that sits beside your **real** terminal and Neovim, and helps you
learn by doing.

```
┌───────────────────┬──────────────────────────────┐
│ Tutor             │ Neovim                       │
│ (chat, hints,     ├──────────────────────────────┤
│  checks)          │ zsh shell                    │
├───────────────────┴──────────────────────────────┤
│ GOAL | EX | OBS: ON | SYNC: current | MODE | MODEL│
└──────────────────────────────────────────────────┘
```

- **You do the work.** The tutor explains, asks you to predict, gives small
  hints, and checks results with trusted code.
- **It watches only what you allow:** the panes of its own tmux session and
  your practice folder (`~/cli-mentor-workspace`). Pause any time.
- **It remembers** your goals, what you have shown you can do, and where you
  stopped. Its own guesses stay "proposed" until you confirm them.
- **Honest:** status words show how fresh its view is; it never claims you
  passed unless a checker says so.

Status: working first release for macOS (Apple Silicon). See
[docs/FEATURES.md](docs/FEATURES.md) for what is done and what is planned.

## Quickstart (macOS)

You need [Homebrew](https://brew.sh) and [uv](https://docs.astral.sh/uv/).

```sh
# 1. Tools (skip any you already have; `mentor doctor` tells you what is missing)
brew install tmux neovim

# 2. Get the project and its Python packages (into the project folder only)
cd climentor
uv sync

# 3. Check your setup (installs nothing)
uv run mentor doctor

# 4. Set up: practice folder, profile, and which tutor model to use
uv run mentor init

# 5. Start learning
uv run mentor start
```

Inside the session, type `/lesson` in the tutor pane (left) to begin.
Move between panes with **Ctrl+b** then an arrow key. Quit with **Ctrl+Q**.

Want to try it without any API key? `uv run mentor demo` uses real tmux,
Neovim and zsh with an offline **mock** tutor (clearly labelled, not an AI).

Optional: put `mentor` on your PATH with `uv tool install --editable .`
(this writes to `~/.local/bin`).

### Using a real AI tutor (bring your own key)

`mentor init` asks for Anthropic or OpenAI, stores the key in the **macOS
Keychain** (or reads `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`), and lets you
pick a model from the provider's own list. Nothing is sent until you approve,
and `/context` shows exactly what would be sent.

## Everyday commands

| Where | Command |
|---|---|
| Terminal | `mentor start`, `mentor resume`, `mentor stop`, `mentor sessions` |
| Tutor pane | `/lesson`, `/hint`, `/explain <term>`, `/check`, `/progress`, `/observe pause` |

Full list: [docs/COMMANDS.md](docs/COMMANDS.md) or `/help`.

## Verify it works (for developers)

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest -m "not live"      # unit + real tmux/zsh/Neovim integration + end-to-end
```

Integration and end-to-end tests use private tmux sockets, temp folders, a
fake HOME, and a fake keyring. They never touch your real data, Keychain, or
tmux server, and never call a paid API. Live provider tests are opt-in:
see `tests/live/test_live_providers.py`.

## Documentation

- [Architecture](docs/ARCHITECTURE.md) · [Threat model](docs/THREAT_MODEL.md) · [Decisions](docs/DECISIONS.md)
- [Commands](docs/COMMANDS.md) · [Curriculum format](docs/CURRICULUM.md) · [Privacy and deletion](docs/PRIVACY.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md) · [Uninstall](docs/UNINSTALL.md) · [Features](docs/FEATURES.md)
- [Status and handoff](STATUS.md) · [Checklist](docs/CHECKLIST.md) · [Example config](config.example.toml)

## Safety in one paragraph

Default is observe-and-discuss. The tutor cannot run commands, install
software, or change databases. Exercise files are created only after you
approve. Python checks run your code on your Mac (not a sandbox) only after
you approve. Secrets are filtered on a best-effort basis. Data already sent
to a provider cannot be recalled. Details: [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).
