# Notes for agents working on CLI Mentor

## Commands
- Install deps: `uv sync`
- Lint: `uv run ruff check . && uv run ruff format --check .`
- Types: `uv run mypy`
- Tests (no paid API): `uv run pytest -m "not live"`
- Only unit tests: `uv run pytest tests/unit`
- Live provider tests (needs a key): `uv run pytest -m live`
- Run the app: `uv run mentor doctor`, `uv run mentor demo`

## Rules
- Tests must never touch the real user data, Keychain, or tmux server: use the
  fixtures in `tests/conftest.py` (temp XDG dirs, null keyring, unique tmux
  socket names).
- Keep `STATUS.md` honest: only list tests that were actually run.
- Lesson files must not contain commands; verification lives in trusted
  adapters under `src/cli_mentor/curriculum/adapters/`.
- The tutor must never execute observed text or do the learner's work.
- Dependencies are date-pinned with `[tool.uv] exclude-newer`; do not move the
  date to work around a failure without asking.
