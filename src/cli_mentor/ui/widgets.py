"""Small widgets shared by the tutor pane and the setup screens."""

from __future__ import annotations

from textual.content import Content
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

Segment = tuple[str, str]  # (text, style); styles may use theme variables such as $accent


class Line(Static):
    """Text built from styled segments (no markup parsing, so AI-written titles are safe)."""

    def show(self, segments: list[Segment]) -> None:
        self.update(Content.assemble(*segments))


class ProgressRule(Widget):
    """A full-width line that fills up: done in the accent colour, the current part brighter."""

    DEFAULT_CSS = "ProgressRule { height: 1; }"
    done: reactive[int] = reactive(0)
    total: reactive[int] = reactive(0)
    current: reactive[int] = reactive(0)

    def render(self) -> Content:
        width = self.size.width
        if not self.total:
            return Content.assemble(("─" * width, "$mentor-rule"))
        filled = min(width, round(width * self.done / self.total))
        reached = min(width, round(width * self.current / self.total)) if self.current else filled
        return Content.assemble(
            ("━" * filled, "$accent"),
            ("━" * max(0, reached - filled), "$foreground 60%"),
            ("─" * max(0, width - max(filled, reached)), "$mentor-rule"),
        )
