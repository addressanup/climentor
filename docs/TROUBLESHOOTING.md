# Troubleshooting

Start with `mentor doctor`. It lists each problem with a fix.

| Symptom | Likely cause | Fix |
|---|---|---|
| `mentor: command not found` | Not installed on your PATH | Use `uv run mentor ...` from the project folder, or install it: `uv tool install --editable .` |
| "You are already inside tmux" | `mentor start` would nest tmux | Open a new terminal window and run the printed command, or use `--nested` |
| Status shows `○ observation off` | Observation was not approved | `/observe resume` |
| Status shows `terminal disconnected` | The tmux observer lost its connection | It reconnects by itself; if not, `/observe pause` then `/observe resume` |
| The tutor never mentions your commands | No command run yet, or zsh hooks could not load | Run any command in the shell pane. `/observe` lists each source. `mentor doctor` checks the `zsh/net/socket` module |
| Status shows `Neovim disconnected` | Neovim in the editor pane was closed, or started without the plugin | Run `nvim` from the session's shell pane (it adds the plugin automatically) |
| Status shows `… incomplete` | Some events were missed (load or a restart) | It recovers with fresh snapshots; shell history before that point is marked incomplete |
| Accent colours are hard to read | CLI Mentor uses your terminal's own colours and guesses light or dark from `COLORFGBG` | `export CLI_MENTOR_THEME=light` (or `dark`) before `mentor start` |
| The setup screen looks broken (over SSH, odd terminal) | The full-screen setup needs a capable terminal | `mentor init --plain` asks the same questions as plain text |
| The tutor pane says "Pane is dead" | The tutor process crashed | `mentor resume` restarts it; editor and shell keep running. See the log below |
| "No API key for anthropic/openai" | Key not in env or Keychain | `mentor key set anthropic` (or `openai`), or export the env var |
| "The provider rejected the request" | Wrong model id | `mentor init` and pick a model from the list, or `/model list` |
| "rate limit reached" | Provider asked us to slow down | Wait the suggested time; retries are already bounded |
| macOS asks to allow Keychain access | First use of the stored key | Click "Allow" (or "Always Allow" for the Python that runs CLI Mentor) |
| Socket path too long | Very long `$TMPDIR` | `export CLI_MENTOR_RUNTIME_DIR=/tmp/cm-$USER` |
| Esc feels slow in Neovim | tmux escape delay | Our tmux config sets `escape-time 10`; your `~/.tmux.conf` may override it |
| Python check says "timed out" | Infinite loop or long-running code | Fix the loop; checks stop after 10 seconds |

## Logs

`~/.local/state/cli-mentor/logs/mentor.log` holds metadata (no observed text,
keys masked). Attach it when reporting a problem; read it first to be sure.

## Clean restart

```sh
mentor stop --force        # closes only CLI Mentor's own tmux session
tmux -L cli-mentor kill-server   # only CLI Mentor's private tmux server
mentor start
```
