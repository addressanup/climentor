"""Logging setup. Logs hold metadata, not observed content, and are scrubbed."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from .apikeys import scrub_keys

_registered_secrets: list[str] = []


def register_secret(value: str | None) -> None:
    """Remember a live secret so it is masked if it ever reaches a log line."""
    if value and value not in _registered_secrets:
        _registered_secrets.append(value)


class ScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = scrub_keys(message, _registered_secrets)
        if cleaned != message:
            record.msg = cleaned
            record.args = None
        return True


def setup_logging(log_file: Path, level: int = logging.INFO) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(ScrubFilter())
    root = logging.getLogger("cli_mentor")
    root.setLevel(level)
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.propagate = False
    try:
        log_file.chmod(0o600)
    except OSError:
        pass
