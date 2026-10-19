# Implementation checklist

Status keys: `[x]` done and tested, `[~]` partly done, `[ ]` not started.
`STATUS.md` has the details, commands run, and test results.

## Phase 0 — setup
- [x] git repo, `pyproject.toml`, `uv.lock`, date-pinned dependencies
- [x] Architecture, threat model, decisions, checklist, status docs

## Phase 1 — working vertical slice
- [ ] paths, config, secrets, SQLite store + migrations
- [ ] permissions core
- [ ] event schema, sequencer, privacy filter, workspace scope
- [ ] IPC server (token auth) + zsh session hooks
- [ ] tmux control-mode client, parser, snapshots, reconnect
- [ ] state projection + sync status
- [ ] provider interface, mock, Anthropic, OpenAI
- [ ] intervention policy, context assembly, staleness, engine
- [ ] curriculum format + one CLI exercise + trusted verifier
- [ ] Textual tutor UI, slash commands, approvals
- [ ] `mentor doctor/init/start/resume/stop/sessions`
- [ ] pause / resume with fresh-snapshot consent

## Phase 2 — Neovim
- [ ] session-scoped Lua plugin (snapshot + incremental lines)
- [ ] mode, cursor, save, diagnostics, reload, detach, restart
- [ ] sensitive / out-of-scope / terminal buffer skipping
- [ ] one verified Neovim exercise

## Phase 3 — personalisation and curricula
- [ ] onboarding confirms or edits seeded preferences
- [ ] skill evidence, assistance levels, skill states
- [ ] review scheduling
- [ ] session resumption with memory
- [ ] 3 CLI, 3 Neovim, 3 Python exercises
- [ ] opt-in Neovim technique telemetry
- [ ] host-execution runner for Python checks (limits, clean env)

## Phase 4 — hardening
- [ ] permission flows in UI and CLI
- [ ] export, selective delete, delete-session, retention purge
- [ ] backpressure and stale-response handling tests
- [ ] failure recovery (provider, tutor restart, stale sockets)
- [ ] end-to-end acceptance tests
- [ ] CI workflow
- [ ] README quickstart, troubleshooting, uninstall, feature matrix
