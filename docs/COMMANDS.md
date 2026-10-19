# Commands

## Terminal commands (`mentor ...`)

Run these in a normal terminal. During development use `uv run mentor ...`
from the project folder.

| Command | What it does |
|---|---|
| `mentor doctor [--json]` | Checks tools, versions, folders, and settings. Prints fixes. Installs nothing. |
| `mentor init [--yes] [--provider mock\|anthropic\|openai] [--model ID] [--workspace PATH] [--base-url URL]` | Setup: practice folder (asks first), profile confirmation, observation approval, provider, key, model. |
| `mentor start [--provider P] [--detach] [--nested]` | Starts a new session in the private tmux server and attaches. `--provider` overrides the config for this session only. |
| `mentor resume [SESSION] [--detach] [--nested]` | Re-attaches. Restarts the tutor pane if it crashed. Rebuilds the panes if tmux was closed; learning state is kept. |
| `mentor stop [SESSION] [--force]` | Closes a session's panes. Refuses while Neovim is open or a command is running, unless `--force`. |
| `mentor sessions [--limit N]` | Lists sessions, whether their tmux session is alive, and the current exercise. |
| `mentor profile [show\|confirm KEY\|all\|reject KEY\|set KEY TEXT\|remove KEY]` | Shows or edits your profile. |
| `mentor memory [list\|search WORDS\|add TEXT\|confirm ID\|reject ID\|forget ID]` | Shows or edits remembered notes. |
| `mentor export [--out FILE] [--include-events] [--no-transcripts]` | Exports your data as JSON. No API keys. |
| `mentor delete-session SESSION [--with-evidence] [--yes]` | Deletes one session's observations, transcript, model suggestions, usage (and evidence if asked), then compacts the database. |
| `mentor key set\|delete\|status anthropic\|openai` | Manages the API key in the macOS Keychain. The value is never printed. |
| `mentor demo [--detach] [--nested]` | Offline demo: real tmux, Neovim and zsh; the MOCK tutor; separate demo data. The mock cannot design learning plans. |
| `mentor --version` | Prints the version. |

`SESSION` can be a full id or a unique prefix (see `mentor sessions`).

## Tutor-pane commands (`/...`)

Type these in the tutor pane (left). Anything that does not start with `/` is
a message to the tutor. The same table is shown by `/help`.

| Command | What it does |
|---|---|
| `/help` | Show this list |
| `/goal [text]` | Show or set your goal for this session |
| `/learn <topic>` | Start a new learning plan, e.g. `/learn C++ from the basics` |
| `/plan` | Show your plan and where you are |
| `/plan accept` | Accept the proposed plan (same as replying “yes”) |
| `/plan skip` | Skip the current step |
| `/plan list` / `switch <id>` | See all your plans / switch to another one |
| `/lesson` | Start or continue the current step |
| `/lesson skip` | Skip the prediction question and show the task |
| `/lesson reset` | Recreate the step's starting files (asks first) |
| `/lesson cleanup` | Remove unchanged files the step created (asks first) |
| `/hint` | Next, slightly stronger hint (recorded as help) |
| `/solution` | Show the full solution (asks; recorded as full help) |
| `/explain [term]` | Explain a word or symbol from the current lesson |
| `/check [answer]` | Check the current step (some steps need an answer) |
| `/review` | Skills due for review, and a review question |
| `/progress` | Your skill states and the evidence behind them |
| `/profile` | Your profile, with where each fact came from |
| `/profile confirm <key>\|all` | Confirm proposed profile facts |
| `/profile reject <key>` | Reject a proposed fact |
| `/profile set <key> <text>` | Add or change a fact in your own words |
| `/profile remove <key>` | Delete a fact |
| `/profile style quiet\|balanced\|proactive` | How often the tutor may speak unasked |
| `/memory` | Remembered notes, with provenance |
| `/memory search <words>` | Search memory |
| `/memory add <text>` | Remember something you state |
| `/memory confirm <id>` / `reject <id>` | Accept or refuse a tutor suggestion |
| `/memory correct <id> <text>` | Replace a note with your correction |
| `/forget <id>` | Delete one memory note |
| `/forget observations` | Delete stored observations for this session |
| `/mode [guided\|practice\|project\|assessment\|review\|quiet]` | Show or change teaching mode |
| `/observe` | Observation status and scope |
| `/observe pause` / `resume` | Stop or restart observation |
| `/permissions` | Active permissions |
| `/permissions revoke <capability\|id>` | Revoke a permission |
| `/context` | Exactly what would be sent to the model (redacted) |
| `/model` | Provider, model, and token usage |
| `/model list` | Models the provider offers |
| `/cancel` | Stop the tutor's current reply (or press Esc) |
| `/quit` | Leave: detach, end the session, or cancel |

## Keys

| Where | Key | Action |
|---|---|---|
| Tutor pane | Enter | Send |
| Tutor pane | Ctrl+J or Alt+Enter | New line in your message |
| Tutor pane | Esc | Cancel the tutor's reply |
| Tutor pane | Ctrl+Q | Quit menu (detach / end / cancel) |
| Tutor pane | Page Up / Page Down | Scroll the conversation |
| Question dialogs | the letter in `[ ]` | Choose; Esc means "no" |
| tmux | Ctrl+b then an arrow | Move to another pane |
| tmux | Ctrl+b then z | Zoom the current pane (again to unzoom) |
| tmux | Ctrl+b then d | Detach (everything keeps running) |
| tmux | mouse drag on a border | Resize panes |

## Teaching modes

| Mode | Behaviour |
|---|---|
| guided | Walks through lesson stages; may offer a small hint after repeated failures. |
| practice | Brief; lets you try first. |
| project | Helps with your own project; asks about goals and trade-offs. |
| assessment | Never helps unasked. `/hint` or `/solution` marks the attempt as assisted. |
| review | Asks review questions for skills that are due. |
| quiet | Speaks only when you ask. |
