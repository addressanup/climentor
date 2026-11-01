"""Approved-folder scope checks and safe file reads.

Every path is expanded and resolved with ``realpath`` before checking, so a
symlink inside an approved folder that points outside it is rejected.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from .privacy import looks_binary, redact, sensitive_reason


@dataclass(frozen=True)
class ScopeDecision:
    allowed: bool
    reason: str
    resolved: Path | None = None


@dataclass(frozen=True)
class FileRead:
    path: Path
    text: str | None
    reason: str
    size: int = 0
    sha256: str | None = None
    redactions: int = 0


class WorkspaceScope:
    def __init__(self, roots: list[Path], max_file_bytes: int = 256 * 1024) -> None:
        self.roots = [Path(os.path.realpath(r)) for r in roots]
        self.max_file_bytes = max_file_bytes

    def root_for(self, resolved: Path) -> Path | None:
        for root in self.roots:
            if resolved == root or root in resolved.parents:
                return root
        return None

    def check(self, path: str | Path, base: Path | None = None) -> ScopeDecision:
        p = Path(path).expanduser()
        if not p.is_absolute():
            if base is None:
                return ScopeDecision(False, "relative path without a base folder")
            p = base / p
        resolved = Path(os.path.realpath(p))
        root = self.root_for(resolved)
        if root is None:
            return ScopeDecision(False, "outside approved folders", resolved)
        relative = resolved.relative_to(root)
        reason = sensitive_reason(relative) if relative.parts else None
        if reason:
            return ScopeDecision(False, reason, resolved)
        return ScopeDecision(True, "in scope", resolved)

    def read_text(self, path: str | Path, base: Path | None = None, *, redact_secrets: bool = True) -> FileRead:
        """Read an in-scope text file. ``redact_secrets=False`` is only for trusted local comparisons
        (the checker); that text is never stored or sent anywhere."""
        decision = self.check(path, base)
        target = decision.resolved or Path(path)
        if not decision.allowed or decision.resolved is None:
            return FileRead(target, None, decision.reason)
        if not decision.resolved.is_file():
            return FileRead(target, None, "not a regular file")
        size = decision.resolved.stat().st_size
        if size > self.max_file_bytes:
            return FileRead(target, None, f"too large ({size} bytes)", size)
        data = decision.resolved.read_bytes()
        if looks_binary(data):
            return FileRead(target, None, "binary file", size)
        digest = hashlib.sha256(data).hexdigest()
        text = data.decode("utf-8", errors="replace")
        if not redact_secrets:
            return FileRead(target, text, "ok", size, digest)
        red = redact(text)
        return FileRead(target, red.text, "ok", size, digest, red.count)

    def list_files(self, folder: Path, limit: int = 200) -> list[str]:
        """Relative paths of in-scope files under ``folder`` (no contents)."""
        decision = self.check(folder)
        if not decision.allowed or decision.resolved is None or not decision.resolved.is_dir():
            return []
        out: list[str] = []
        base = decision.resolved
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            current = Path(dirpath)
            dirnames[:] = sorted(d for d in dirnames if self.check(current / d).allowed)
            for name in sorted(filenames):
                full = current / name
                if self.check(full).allowed:
                    out.append(str(full.relative_to(base)))
                    if len(out) >= limit:
                        return out
        return out
