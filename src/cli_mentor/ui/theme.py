"""Colour choices shared by the tutor pane, the setup screens, and the tmux bars.

CLI Mentor uses the terminal's own colours, so it matches Neovim and the shell.
The only thing it needs to know is whether the background is light or dark, to
pick an accent colour that is easy to read.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.theme import Theme


def terminal_is_light(env: Mapping[str, str] = os.environ) -> bool:
    """Guess whether the terminal has a light background.

    ``CLI_MENTOR_THEME=light`` or ``dark`` decides. Otherwise we read
    ``COLORFGBG``, which many terminals set (``0;15`` = dark text on white).
    """
    choice = env.get("CLI_MENTOR_THEME", "").strip().lower()
    if choice in ("light", "dark"):
        return choice == "light"
    return env.get("COLORFGBG", "").rsplit(";", 1)[-1] in ("7", "15")


def accent_name(light: bool) -> str:
    """The accent as a plain ANSI colour name (tmux and Textual both understand it)."""
    return "blue" if light else "cyan"


def mentor_theme(light: bool) -> Theme:
    """A Textual theme: the terminal's own colours (ANSI), plus one accent colour for structure."""
    from textual.theme import BUILTIN_THEMES  # imported here so tmux code does not load Textual

    base = BUILTIN_THEMES["ansi-light" if light else "ansi-dark"]
    accent = f"ansi_{accent_name(light)}"
    return dataclasses.replace(
        base,
        name="cli-mentor",
        primary=accent,
        secondary=accent,
        accent=accent,
        variables={
            **base.variables,
            "mentor-code": "ansi_magenta" if light else "ansi_yellow",
            "mentor-rule": "ansi_bright_black",
            "border": accent,
            "border-blurred": "ansi_bright_black",
            **{f"markdown-h{n}-background": "transparent" for n in range(1, 7)},
            **{f"markdown-h{n}-text-style": "bold" for n in range(1, 7)},
            "markdown-h1-color": accent,
            "markdown-h2-color": accent,
            **{f"markdown-h{n}-color": "ansi_default" for n in range(3, 7)},
        },
    )
