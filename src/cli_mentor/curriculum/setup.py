"""Create and clean up exercise files inside the approved workspace.

Setup never overwrites an existing file unless asked to reset. Cleanup only
removes files that we created *and* that still have the content we wrote.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..db.store import Store
from ..observe.workspace import WorkspaceScope
from .model import Lesson


@dataclass
class SetupPlan:
    lesson: Lesson
    workdir: Path
    to_create: list[tuple[Path, str | None]] = field(default_factory=list)  # (path, content|None=dir)
    existing: list[Path] = field(default_factory=list)

    def describe(self, preview_lines: int = 15) -> str:
        """What will be created, with the start of each file: the tutor wrote it, and it may run later."""
        lines = [f"Folder: {self.workdir}"]
        for p, content in self.to_create:
            lines.append(f"  create {p.relative_to(self.workdir)}")
            rows = (content or "").splitlines()
            lines += [f"    │ {row}" for row in rows[:preview_lines]]
            if len(rows) > preview_lines:
                lines.append(f"    │ … {len(rows) - preview_lines} more lines")
        lines += [f"  keep existing {p.relative_to(self.workdir)}" for p in self.existing]
        return "\n".join(lines)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def plan_setup(lesson: Lesson, workspace_root: Path, scope: WorkspaceScope, reset: bool = False) -> SetupPlan:
    workdir = workspace_root / lesson.workdir
    if not scope.check(workdir).allowed and not scope.check(workspace_root).allowed:
        raise PermissionError(f"{workdir} is outside the approved workspace")
    plan = SetupPlan(lesson, workdir)
    for item in lesson.setup:
        target = workdir / item.path
        resolved = Path(os.path.realpath(target))
        if scope.root_for(resolved) is None:
            raise PermissionError(f"setup path escapes the workspace: {item.path}")
        if item.kind == "dir":
            if not target.is_dir():
                plan.to_create.append((target, None))
            continue
        content = item.content
        content = content.lstrip("\n") if content.startswith("\n") else content
        if target.exists() and not reset:
            plan.existing.append(target)
        else:
            plan.to_create.append((target, content))
    return plan


def apply_setup(plan: SetupPlan, store: Store) -> list[Path]:
    created: list[Path] = []
    plan.workdir.mkdir(parents=True, exist_ok=True)
    for path, content in plan.to_create:
        if content is None:
            path.mkdir(parents=True, exist_ok=True)
            created.append(path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        created.append(path)
        store.conn.execute(
            "INSERT INTO exercise_files(exercise_id, path, sha256, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(exercise_id, path) DO UPDATE SET sha256 = excluded.sha256, created_at = excluded.created_at",
            (plan.lesson.id, str(path), _sha(content), time.time()),
        )
    return created


def cleanup(lesson: Lesson, workspace_root: Path, store: Store) -> list[tuple[str, str]]:
    """Remove unchanged files we created. Returns (path, action) pairs."""
    report: list[tuple[str, str]] = []
    rows = store.conn.execute("SELECT path, sha256 FROM exercise_files WHERE exercise_id = ?", (lesson.id,)).fetchall()
    workdir = workspace_root / lesson.workdir
    our_dirs: set[Path] = set()
    for row in rows:
        path = Path(row["path"])
        parent = path.parent
        while parent != workdir.parent and workdir in (parent, *parent.parents):
            our_dirs.add(parent)
            parent = parent.parent
        if not path.exists():
            report.append((str(path), "already gone"))
        elif path.is_file() and _sha(path.read_text(encoding="utf-8", errors="replace")) == row["sha256"]:
            path.unlink()
            report.append((str(path), "removed"))
        else:
            report.append((str(path), "kept (you changed it)"))
            continue
        store.conn.execute("DELETE FROM exercise_files WHERE exercise_id = ? AND path = ?", (lesson.id, str(path)))
    # Remove folders that held our files, if they are now empty (deepest first).
    for folder in sorted(our_dirs, key=lambda p: -len(p.parts)):
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    return report
