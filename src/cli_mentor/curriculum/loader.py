"""Load and validate the built-in lessons."""

from __future__ import annotations

import tomllib
from functools import cache
from importlib import resources

from .model import Lesson
from .verify import GENERATORS, REGISTRY, load_adapters

TRACK_TITLES = {
    "cli": "CLI fundamentals",
    "nvim": "Neovim fundamentals",
    "python": "Python fundamentals",
}


class CurriculumError(ValueError):
    pass


@cache
def load_lessons() -> dict[str, Lesson]:
    load_adapters()
    root = resources.files("cli_mentor.curriculum").joinpath("tracks")
    lessons: dict[str, Lesson] = {}
    for track_dir in sorted(root.iterdir(), key=lambda p: p.name):
        if not track_dir.is_dir():
            continue
        for entry in sorted(track_dir.iterdir(), key=lambda p: p.name):
            if not entry.name.endswith(".toml"):
                continue
            try:
                lesson = Lesson.model_validate(tomllib.loads(entry.read_text(encoding="utf-8")))
            except Exception as exc:
                raise CurriculumError(f"{track_dir.name}/{entry.name}: {exc}") from exc
            if lesson.id in lessons:
                raise CurriculumError(f"duplicate lesson id {lesson.id}")
            if lesson.verifier not in REGISTRY:
                raise CurriculumError(f"{lesson.id}: unknown verifier {lesson.verifier}")
            for item in lesson.setup:
                if item.generator and item.generator not in GENERATORS:
                    raise CurriculumError(f"{lesson.id}: unknown generator {item.generator}")
            lessons[lesson.id] = lesson
    for lesson in lessons.values():
        for pre in lesson.prerequisites:
            if pre not in lessons:
                raise CurriculumError(f"{lesson.id}: unknown prerequisite {pre}")
    return lessons


def lessons_by_track() -> dict[str, list[Lesson]]:
    tracks: dict[str, list[Lesson]] = {}
    for lesson in load_lessons().values():
        tracks.setdefault(lesson.track, []).append(lesson)
    for items in tracks.values():
        items.sort(key=lambda lesson: lesson.order)
    return dict(
        sorted(
            tracks.items(),
            key=lambda kv: list(TRACK_TITLES).index(kv[0]) if kv[0] in TRACK_TITLES else 99,
        )
    )


def find_lesson(query: str) -> Lesson | None:
    lessons = load_lessons()
    if query in lessons:
        return lessons[query]
    matches = [lesson for lid, lesson in lessons.items() if lid.endswith("." + query) or lid.startswith(query)]
    return matches[0] if len(matches) == 1 else None


def next_lesson(completed: set[str], track: str | None = None) -> Lesson | None:
    for name, items in lessons_by_track().items():
        if track and name != track:
            continue
        for lesson in items:
            if lesson.id not in completed and all(p in completed for p in lesson.prerequisites):
                return lesson
    return None
