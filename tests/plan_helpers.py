"""Shared helpers for plan-based tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cli_mentor.curriculum.model import Lesson
from cli_mentor.curriculum.plans import PlanOutline, PlanStore, StepDetails, build_lesson
from cli_mentor.db.store import Store
from cli_mentor.providers.mock import MockBehaviour, MockProvider

FIXTURE = Path(__file__).parent / "fixtures" / "cpp_plan.json"


def fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    return data


def planning_mock(**kw: Any) -> MockProvider:
    behaviour = MockBehaviour.from_fixture(FIXTURE)
    for key, value in kw.items():
        setattr(behaviour, key, value)
    return MockProvider(behaviour=behaviour)


def lesson_for_step(store: Store, index: int = 0, **detail_overrides: Any) -> Lesson:
    """Create an active plan from the fixture and build the lesson for one step."""
    data = fixture()
    plans = PlanStore(store)
    plan = plans.activate(plans.create("learn C++", PlanOutline.model_validate(data["plan"])).id)
    step = plans.steps(plan.id)[index]
    details = dict(data["steps"][index])
    details.update(detail_overrides)
    lesson = build_lesson(plan, step, StepDetails.model_validate(details))
    plans.save_lesson(plan.id, step.step_id, lesson)
    return lesson
