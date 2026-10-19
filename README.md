# CLI Mentor

A tutor that sits beside your **real** terminal and Neovim, and helps you
learn by doing.

```
┌─ Mentor ───────────────────────┬─ Editor · nvim ─────────────────┐
│ C++ from the very basics       │                                 │
│ Hello, C++    ● learn ○ try ○ c│  your code                      │
│ ━━━━━━━━━━──────────────────── │                                 │
│ Mentor · lesson                ├─ Shell · zsh ───────────────────┤
│ New words: …                   │                                 │
│ ╭────────────────────────────╮ │  $ c++ hello.cpp -o hello       │
│ │ Your prediction: …         │ │                                 │
│ ╰────────────────────────────╯ │                                 │
└────────────────────────────────┴─────────────────────────────────┘
 CLI Mentor     C++ from the very basics · step 1/3 · ● observing · deepseek-chat
```

- **You choose what to learn.** The tutor asks "What would you like to
  learn?", turns your answer (say, "C++ from the very basics") into a
  step-by-step plan, and you accept or change it.
- **You do the work.** For each step it explains every new word and symbol,
  asks you to predict, gives small hints, and checks results with trusted code.
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

# 4. Set up (a full-screen guide: practice folder, about you, AI model)
uv run mentor init

# 5. Start learning (step 4 offers to do this for you)
uv run mentor start
```

`mentor init` walks you through five short steps: Enter continues, Space
ticks a box, Esc goes back. Over SSH or in scripts, use `mentor init --plain`.

Your first session starts with a short tour of the screen. Then the tutor asks
"What would you like to learn?" in the tutor pane (left). It proposes a plan;
reply **yes** to start. The header always shows the subject, the step you are
on, and where you are in it (learn, try, check). `/tour` shows the tour again.
Each subject gets its own folder and conversation. Next time, `mentor start`
asks which subject to continue (or start a new one); `mentor subjects` lists them.
Move between panes with **Ctrl+b** then an arrow key. Quit with **Ctrl+Q**.

Learning plans need a real AI model: your ChatGPT plan or your own API key.
`uv run mentor demo` shows the program with an offline **mock** tutor, but the
mock cannot design plans.

Optional: put `mentor` on your PATH with `uv tool install --editable .`
(this writes to `~/.local/bin`).

### Using your ChatGPT plan (no API key)

Choose **ChatGPT plan** in `mentor init` (or run `uv run mentor login`). Your
browser opens; sign in to ChatGPT and allow CLI Mentor to use your plan. This
is OpenAI's official "Sign in with ChatGPT" for open-source apps, and it draws
on the same plan usage as Codex. Then pick a model from your plan's list.

- The sign-in is kept in the **macOS Keychain** and renews by itself.
- Tutor replies count toward your plan. See or limit them at
  <https://chatgpt.com/settings/usage>.
- `mentor logout` signs out (`--forget` also lets you switch accounts).

### Using a real AI tutor (bring your own key)

`mentor init` also offers Anthropic, OpenAI, or another service that works like
OpenAI (for example DeepSeek: type its address, such as
`https://api.deepseek.com`). It stores the key in the **macOS Keychain** (or
reads `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`) and lets you pick a model from
the service's own list. Nothing is sent until you approve, and `/context`
shows exactly what would be sent.

The tutor pane and tmux bars use your terminal's own colours. If the accent
colour is hard to read, set `CLI_MENTOR_THEME=light` (or `dark`).

## Everyday commands

| Where | What to type |
|---|---|
| Terminal | `mentor start`, `mentor resume`, `mentor stop`, `mentor sessions` |
| Tutor pane | Plain English. "done" when you finish (your work is checked), "hint" if you are stuck, "next" to move on, or any question. No commands needed |

Optional shortcuts and the full list: [docs/COMMANDS.md](docs/COMMANDS.md) or `/help`.

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
- [Commands](docs/COMMANDS.md) · [Learning plans](docs/CURRICULUM.md) · [Privacy and deletion](docs/PRIVACY.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md) · [Uninstall](docs/UNINSTALL.md) · [Features](docs/FEATURES.md)
- [Status and handoff](STATUS.md) · [Checklist](docs/CHECKLIST.md) · [Example config](config.example.toml)

## Safety in one paragraph

Default is observe-and-discuss. The tutor cannot run commands, install
software, or change databases. Exercise files are created only after you
approve. Python checks run your code on your Mac (not a sandbox) only after
you approve. Secrets are filtered on a best-effort basis. Data already sent
to a provider cannot be recalled. Details: [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).
