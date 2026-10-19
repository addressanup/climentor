"""Capabilities and scoped, revocable grants.

Decisions are deterministic and data-driven. Only explicit learner approval
(UI or CLI) can create a grant. Model output can never call ``grant``.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .db.store import Store


class Capability(StrEnum):
    OBSERVE_TERMINAL = "observe_terminal"
    OBSERVE_EDITOR = "observe_editor"
    READ_FILES = "read_files"
    RUN_CHECKS = "run_checks"  # trusted checks that do not run learner code
    RUN_LEARNER_CODE = "run_learner_code"  # host execution of learner code
    MODIFY_FILES = "modify_files"  # exercise setup / cleanup only
    RUN_SHELL = "run_shell"
    MUTATE_DATABASE = "mutate_database"
    INSTALL_SOFTWARE = "install_software"
    NETWORK = "network"


class Scope(StrEnum):
    ONCE = "once"
    SESSION = "session"
    PERSISTENT = "persistent"


DESCRIPTIONS: dict[Capability, str] = {
    Capability.OBSERVE_TERMINAL: "See output of the shell pane in this session",
    Capability.OBSERVE_EDITOR: "See Neovim buffers (including unsaved edits) in approved folders",
    Capability.READ_FILES: "Read files inside approved folders",
    Capability.RUN_CHECKS: "Run trusted checks that read files and observed records",
    Capability.RUN_LEARNER_CODE: "Run YOUR code on this Mac to check it (host execution, not a sandbox)",
    Capability.MODIFY_FILES: "Create or remove exercise files inside approved folders",
    Capability.RUN_SHELL: "Run shell commands (not available to the tutor in this version)",
    Capability.MUTATE_DATABASE: "Change databases (no current exercise needs this)",
    Capability.INSTALL_SOFTWARE: "Install software (never done automatically)",
    Capability.NETWORK: "Send context to the configured model provider",
}

# Capabilities this version refuses to grant at all.
NEVER_GRANTABLE: frozenset[Capability] = frozenset(
    {Capability.RUN_SHELL, Capability.INSTALL_SOFTWARE, Capability.MUTATE_DATABASE}
)
# Capabilities whose resource is a filesystem path (grant covers a folder).
PATH_SCOPED: frozenset[Capability] = frozenset(
    {
        Capability.READ_FILES,
        Capability.OBSERVE_EDITOR,
        Capability.RUN_CHECKS,
        Capability.RUN_LEARNER_CODE,
        Capability.MODIFY_FILES,
    }
)


class PermissionError_(Exception):
    """Raised when a capability cannot be granted."""


@dataclass(frozen=True)
class Grant:
    id: int
    capability: Capability
    scope: Scope
    resource: str | None
    session_id: str | None
    granted_at: float


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    grant_id: int | None = None

    @property
    def needs_approval(self) -> bool:
        return not self.allowed and not self.reason.startswith("never")


def _within(child: str, parent: str) -> bool:
    c = os.path.realpath(child)
    p = os.path.realpath(parent)
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


class PermissionManager:
    def __init__(self, store: Store, session_id: str, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.session_id = session_id
        self.clock = clock

    def active_grants(self) -> list[Grant]:
        rows = self.store.conn.execute(
            "SELECT * FROM permissions WHERE revoked_at IS NULL "
            "AND (scope = 'persistent' OR session_id = ?) "
            "AND NOT (scope = 'once' AND used_at IS NOT NULL) ORDER BY id",
            (self.session_id,),
        ).fetchall()
        return [
            Grant(
                id=r["id"],
                capability=Capability(r["capability"]),
                scope=Scope(r["scope"]),
                resource=r["resource"],
                session_id=r["session_id"],
                granted_at=r["granted_at"],
            )
            for r in rows
        ]

    def check(self, capability: Capability, resource: str | Path | None = None) -> Decision:
        if capability in NEVER_GRANTABLE:
            return Decision(False, f"never: {capability.value} is not available in this version")
        res = str(resource) if resource is not None else None
        for grant in self.active_grants():
            if grant.capability != capability:
                continue
            if capability in PATH_SCOPED:
                if res is None or grant.resource is None or not _within(res, grant.resource):
                    continue
            elif grant.resource is not None and grant.resource != res:
                continue
            return Decision(True, f"granted ({grant.scope.value})", grant.id)
        return Decision(False, f"no grant for {capability.value}" + (f" on {res}" if res else ""))

    def grant(self, capability: Capability, scope: Scope, resource: str | Path | None = None) -> int:
        if capability in NEVER_GRANTABLE:
            raise PermissionError_(f"{capability.value} cannot be granted in this version")
        res = str(resource) if resource is not None else None
        if capability in PATH_SCOPED:
            if res is None:
                raise PermissionError_(f"{capability.value} needs a folder")
            res = os.path.realpath(res)
        with self.store.tx() as conn:
            cur = conn.execute(
                "INSERT INTO permissions(capability, scope, resource, session_id, granted_at) VALUES (?, ?, ?, ?, ?)",
                (capability.value, scope.value, res, self.session_id, self.clock()),
            )
        return int(cur.lastrowid or 0)

    def consume(self, decision: Decision) -> None:
        """Mark a one-time grant as used."""
        if decision.grant_id is None:
            return
        self.store.conn.execute(
            "UPDATE permissions SET used_at = ? WHERE id = ? AND scope = 'once'",
            (self.clock(), decision.grant_id),
        )

    def revoke(self, capability: Capability | None = None, grant_id: int | None = None) -> int:
        if capability is None and grant_id is None:
            raise ValueError("revoke needs a capability or a grant id")
        sql = "UPDATE permissions SET revoked_at = ? WHERE revoked_at IS NULL"
        params: list[object] = [self.clock()]
        if capability is not None:
            sql += " AND capability = ?"
            params.append(capability.value)
        if grant_id is not None:
            sql += " AND id = ?"
            params.append(grant_id)
        cur = self.store.conn.execute(sql, params)
        return cur.rowcount
