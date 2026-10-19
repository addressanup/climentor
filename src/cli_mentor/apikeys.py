"""API key lookup: environment variable first, then the macOS Keychain.

Keys are only read when a provider client is built. They are never written to
config, the database, logs, exports, or model context.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

SERVICE = "cli-mentor"
ENV_VARS: dict[str, str] = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
KeySource = Literal["env", "keychain", "none"]

# Patterns used to scrub anything that looks like a provider key from logs and
# displayed text. Best effort only.
KEY_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{16,}"),
]


@dataclass(frozen=True)
class KeyLookup:
    key: str | None
    source: KeySource

    def __repr__(self) -> str:  # never print the key itself
        return f"KeyLookup(source={self.source!r}, present={self.key is not None})"


def _keyring() -> object | None:
    try:
        import keyring
    except Exception:  # pragma: no cover - keyring is a dependency
        return None
    return keyring


def get_api_key(provider: str, env: Mapping[str, str] | None = None) -> KeyLookup:
    env = os.environ if env is None else env
    var = ENV_VARS.get(provider)
    if var and env.get(var):
        return KeyLookup(env[var], "env")
    kr = _keyring()
    if kr is not None:
        try:
            value = kr.get_password(SERVICE, provider)  # type: ignore[attr-defined]
        except Exception:
            value = None
        if value:
            return KeyLookup(str(value), "keychain")
    return KeyLookup(None, "none")


def set_api_key(provider: str, key: str) -> None:
    kr = _keyring()
    if kr is None:
        raise RuntimeError("The keyring library is not available.")
    kr.set_password(SERVICE, provider, key.strip())  # type: ignore[attr-defined]


def delete_api_key(provider: str) -> bool:
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.delete_password(SERVICE, provider)  # type: ignore[attr-defined]
    except Exception:
        return False
    return True


def keychain_backend_name() -> str:
    kr = _keyring()
    if kr is None:
        return "unavailable"
    try:
        backend = kr.get_keyring()  # type: ignore[attr-defined]
    except Exception as exc:
        return f"error: {type(exc).__name__}"
    return f"{type(backend).__module__}.{type(backend).__name__}"


def scrub_keys(text: str, extra_secrets: list[str] | None = None) -> str:
    for secret in extra_secrets or []:
        if secret and len(secret) >= 8:
            text = text.replace(secret, "[REDACTED-KEY]")
    for pattern in KEY_PATTERNS:
        text = pattern.sub("[REDACTED-KEY]", text)
    return text
