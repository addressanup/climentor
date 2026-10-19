# Uninstall

CLI Mentor never edits your shell or editor configuration, so there is nothing
to undo there. These steps remove everything it created. Read each command
before running it.

1. Stop sessions (only CLI Mentor's private tmux server is affected):

   ```sh
   mentor stop --force
   tmux -L cli-mentor kill-server
   ```

2. Remove the API keys and the ChatGPT sign-in from the Keychain:

   ```sh
   mentor key delete anthropic
   mentor key delete openai
   mentor logout --forget
   ```

   To be sure, also disconnect CLI Mentor at <https://chatgpt.com/settings/usage>.

3. Remove the command (if you installed it with uv):

   ```sh
   uv tool uninstall cli-mentor
   ```

4. Remove data, settings, logs, and caches:

   ```sh
   rm -rf ~/.config/cli-mentor ~/.local/share/cli-mentor ~/.local/state/cli-mentor ~/.cache/cli-mentor
   rm -rf "${TMPDIR:-/tmp}"/cli-mentor-$(id -u)
   ```

5. Optional: remove the practice folder (it holds YOUR exercise work):

   ```sh
   rm -rf ~/cli-mentor-workspace
   ```

6. Optional: delete the project folder you cloned.

Backups (for example Time Machine) may still contain copies. Data already sent
to a model provider is governed by that provider's retention policy.
