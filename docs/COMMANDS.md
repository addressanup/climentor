# Commands

## Terminal commands (`mentor ...`)

Run these in a normal terminal. During development use `uv run mentor ...`
from the project folder.

| Command | What it does |
|---|---|
| `mentor doctor [--json]` | Checks tools, versions, folders, and settings. Prints fixes. Installs nothing. |
| `mentor init [--yes] [--plain] [--provider mock\|chatgpt\|anthropic\|openai] [--model ID] [--workspace PATH] [--base-url URL]` | Setup in five short steps: practice folder, about you (confirm, edit, or remove each guess), how often the tutor speaks up, AI model (your ChatGPT plan through "Continue with ChatGPT", Anthropic, OpenAI, an OpenAI-compatible service such as DeepSeek, or skip), review. At a terminal it opens full screen (Enter continues, Space ticks, ↑ ↓ choose, Esc goes back, Ctrl+C quits without saving) and offers to start your first session at the end. `--plain` asks plain questions instead; `--yes` accepts defaults (for scripts). |
| `mentor start [--subject N\|NAME] [--new] [--provider P] [--detach] [--nested]` | First time: opens the setup above, then your first session. After that: if you have subjects, asks which one to continue (or a new one), then opens its session with the panes in that subject's folder. `--subject` picks without asking, `--new` starts a new subject. `--provider` overrides the config for this session only. |
| `mentor subjects` | Lists your learning subjects, their progress, and their folders. |
| `mentor resume [SESSION] [--detach] [--nested]` | Re-attaches. Restarts the tutor pane if it crashed. Rebuilds the panes if tmux was closed; learning state is kept. |
| `mentor stop [SESSION] [--force]` | Closes a session's panes. Refuses while Neovim is open or a command is running, unless `--force`. |
| `mentor sessions [--limit N]` | Lists sessions, whether their tmux session is alive, and the current exercise. |
| `mentor profile [show\|confirm KEY\|all\|reject KEY\|set KEY TEXT\|remove KEY]` | Shows or edits your profile. |
| `mentor memory [list\|search WORDS\|add TEXT\|confirm ID\|reject ID\|forget ID]` | Shows or edits remembered notes. |
| `mentor export [--out FILE] [--include-events] [--no-transcripts]` | Exports your data as JSON. No API keys. |
| `mentor delete-session SESSION [--with-evidence] [--yes]` | Deletes one session's observations, transcript, model suggestions, usage (and evidence if asked), then compacts the database. |
| `mentor key set\|delete\|status anthropic\|openai` | Manages the API key in the macOS Keychain. The value is never printed. |
| `mentor login` | Signs in with ChatGPT so the tutor uses your ChatGPT plan (no API key). Opens your browser; you sign in and allow CLI Mentor to use your plan. The sign-in is kept in the macOS Keychain and renews by itself. Manage usage at https://chatgpt.com/settings/usage. |
| `mentor logout [--forget]` | Signs out of ChatGPT: asks OpenAI to end the sign-in, then removes it from the Keychain. `--forget` also forgets CLI Mentor's app registration, so the next `mentor login` can use a different ChatGPT account. |
| `mentor demo [--detach] [--nested]` | Offline demo: real tmux, Neovim and zsh; the MOCK tutor; separate demo data. The mock cannot design learning plans. |
| `mentor --version` | Prints the version. |

`SESSION` can be a full id or a unique prefix (see `mentor sessions`).

## Talking to the tutor (no commands needed)

Just type in plain English in the tutor pane (left). The tutor checks your
work as you go:

| You say (any similar words) | What happens |
|---|---|
| "done", "I think I'm done", "check it" | Your work is checked by trusted code |
| An answer to the step's question, e.g. "int" or "I think it's an int" | Your answer is checked |
| "hint", "I'm stuck" | The next, slightly stronger hint (recorded as help) |
| "next", "ready", "continue", "let's go" | The next step starts |
| "skip" | Skips the prediction question, or the current step |
| "show the plan" | Shows your plan and where you are |
| "show me the solution" | Shows the solution (asks first; recorded as full help) |
| "new subject: Rust" | Starts a new learning plan |

Short messages like these are handled directly. For anything longer, the AI
tutor decides: if you say you have finished or give an answer, it runs the
check itself. If you allowed running your code "for this session", running
your program successfully in the step folder also checks the step.

## Tutor-pane shortcuts (`/...`)

Optional. Anything that does not start with `/` is a message to the tutor.
The same table is shown by `/help`.

| Command | What it does |
|---|---|
| `/help` | Show this list |
| `/tour` | A short walkthrough of the screen and how we work |
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
| Question dialogs | the letter shown, or ↑ ↓ then Enter | Choose; Esc means "no" (or "cancel") |
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
