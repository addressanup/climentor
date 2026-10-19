# Implementation checklist

Status keys: `[x]` done and tested, `[~]` partly done, `[ ]` not started.
`STATUS.md` has the details, commands run, and test results.

## Phase 0 — setup
- [x] git repo, `pyproject.toml`, `uv.lock`, date-pinned dependencies
- [x] Architecture, threat model, decisions, checklist, status docs

## Phase 1 — working vertical slice
- [x] paths, config, secrets, SQLite store + migrations
- [x] permissions core
- [x] event schema, sequencer, privacy filter, workspace scope
- [x] IPC server (token auth) + zsh session hooks
- [x] tmux control-mode client, parser, snapshots, reconnect
- [x] state projection + sync status
- [x] provider interface, mock, Anthropic, OpenAI
- [x] intervention policy, context assembly, staleness, engine
- [x] curriculum format + one CLI exercise + trusted verifier
- [x] Textual tutor UI, slash commands, approvals
- [x] `mentor doctor/init/start/resume/stop/sessions`
- [x] pause / resume with fresh-snapshot consent

## Phase 2 — Neovim
- [x] session-scoped Lua plugin (snapshot + incremental lines)
- [x] mode, cursor, save, diagnostics, reload, detach, restart
- [x] sensitive / out-of-scope / terminal buffer skipping
- [x] one verified Neovim exercise

## Phase 3 — personalisation and curricula
- [x] onboarding confirms or edits seeded preferences
- [x] skill evidence, assistance levels, skill states
- [x] review scheduling
- [x] session resumption with memory
- [x] 3 CLI, 3 Neovim, 3 Python exercises
- [x] opt-in Neovim technique telemetry
- [x] host-execution runner for Python checks (limits, clean env)

## Phase 4 — hardening
- [x] permission flows in UI and CLI
- [x] export, selective delete, delete-session, retention purge
- [x] backpressure and stale-response handling tests
- [x] failure recovery (provider, tutor restart, stale sockets)
- [x] end-to-end acceptance tests (stale-advice criterion covered at engine level, not through the UI)
- [~] CI workflow (written; not yet run on GitHub — no remote configured)
- [x] README quickstart, troubleshooting, uninstall, feature matrix

## Not done (see docs/FEATURES.md "Planned")
- [ ] Linux verification
- [ ] bash/fish hooks
- [ ] Git, PostgreSQL, debugging, other-language tracks
- [ ] Strong sandbox for learner code
- [ ] Live provider test run (needs your API key)
