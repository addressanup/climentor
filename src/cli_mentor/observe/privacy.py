"""Best-effort privacy filtering.

Order of defence: capture less first (scope, deny lists, skipped buffers),
then redact what is left. Redaction is best effort, never a guarantee.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePath

from ..apikeys import KEY_PATTERNS

_SECRET_WORDS = r"(?:PASSWORD|PASSWD|PASSPHRASE|SECRET|TOKEN|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY)"
# Only redact values that look like literal secrets (quoted, or 8+ token chars),
# so ordinary code such as ``tokens = line.split()`` is left alone.
_SECRET_VALUE = r"(\"[^\"\n]{3,}\"|'[^'\n]{3,}'|[A-Za-z0-9+/_\-=]{8,}(?=\s|$|[;,\"']))"

_RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
            re.S,
        ),
        "[REDACTED-PRIVATE-KEY]",
    ),
    ("aws_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED-AWS-KEY]"),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})\b"),
        "[REDACTED-TOKEN]",
    ),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED-TOKEN]"),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "[REDACTED-TOKEN]"),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        "[REDACTED-JWT]",
    ),
    (
        "bearer",
        re.compile(r"(?i)(\bauthorization:\s*(?:bearer|basic|token)\s+)\S+"),
        r"\1[REDACTED]",
    ),
    (
        "url_credentials",
        re.compile(r"(\b[a-z][a-z0-9+.-]*://[^:/\s@]+:)[^@\s/]+(@)"),
        r"\1[REDACTED]\2",
    ),
    (
        "assignment",
        re.compile(r"(?i)(\b[A-Z0-9_]*" + _SECRET_WORDS + r"[A-Z0-9_]*\s*[=:]\s*)" + _SECRET_VALUE),
        r"\1[REDACTED]",
    ),
    (
        "cli_flag",
        re.compile(r"(?i)(--?(?:password|passwd|token|api-key|apikey|secret)(?:=|\s+))\S+"),
        r"\1[REDACTED]",
    ),
]
_RULES += [("provider_key", p, "[REDACTED-KEY]") for p in KEY_PATTERNS]

_PASSWORD_PROMPT = re.compile(r"(?i)(password|passphrase|passcode|pin)[^:\n]{0,40}:\s*$")


@dataclass
class Redaction:
    text: str
    count: int = 0
    kinds: list[str] = field(default_factory=list)


def redact(text: str) -> Redaction:
    result = Redaction(text=text)
    for name, pattern, replacement in _RULES:
        new_text, n = pattern.subn(replacement, result.text)
        if n:
            result.text = new_text
            result.count += n
            result.kinds.append(name)
    return result


def redact_text(text: str) -> str:
    return redact(text).text


def is_password_prompt(line: str) -> bool:
    return bool(_PASSWORD_PROMPT.search(line))


# ---------------------------------------------------------------- paths
SENSITIVE_NAME_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"^\.env(\..*)?$",
        r"^\.envrc$",
        r".*\.(pem|key|p12|pfx|kdbx|keychain|gpg|asc)$",
        r"^id_[a-z0-9_]+(\.pub)?$",
        r"^\.netrc$",
        r"^\.pgpass$",
        r"^\.npmrc$",
        r"^\.pypirc$",
        r"^credentials(\..*)?$",
        r"^secrets?(\..*)?$",
        r".*\.sqlite3?$",
        r".*_history$",
    )
]
DENIED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
        "dist",
        "build",
        "target",
        ".ssh",
        ".gnupg",
        ".aws",
        ".kube",
        ".docker",
    }
)


def sensitive_reason(path: str | PurePath) -> str | None:
    """Return why a path must not be captured, or None if it is fine."""
    p = PurePath(path)
    for part in p.parts[:-1]:
        if part in DENIED_DIRS:
            return f"inside excluded folder '{part}'"
    name = p.name
    if name in DENIED_DIRS:
        return f"excluded folder '{name}'"
    for pattern in SENSITIVE_NAME_PATTERNS:
        if pattern.match(name):
            return "name looks sensitive"
    return None


def looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]
