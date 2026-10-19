"""Validated settings stored in ``config.toml``.

Secrets never live here. API keys come from the macOS Keychain or the
environment (see ``secrets.py``).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

import tomlkit
from pydantic import BaseModel, ConfigDict, Field

from .paths import write_private_file

ProviderName = Literal["mock", "anthropic", "openai"]
TeachingMode = Literal["guided", "practice", "project", "assessment", "review", "quiet"]

DEFAULT_WORKSPACE = "~/cli-mentor-workspace"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ProviderSettings(_Model):
    model: str = ""
    base_url: str = ""
    max_output_tokens: int = Field(default=1024, ge=64, le=32000)
    max_context_tokens: int = Field(default=24000, ge=2000, le=1_000_000)
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    max_retries: int = Field(default=2, ge=0, le=5)
    # Optional, learner-provided prices (USD per million tokens). Used only to
    # show an *estimated* cost. We never hard-code prices because they change.
    input_price_per_mtok: float | None = None
    output_price_per_mtok: float | None = None


class TutorSettings(_Model):
    provider: ProviderName = "mock"
    mode: TeachingMode = "guided"
    unsolicited_cooldown_seconds: float = Field(default=60.0, ge=0)
    debounce_seconds: float = Field(default=2.0, ge=0)
    repeated_failure_threshold: int = Field(default=2, ge=1, le=10)


class ObservationSettings(_Model):
    max_file_bytes: int = Field(default=256 * 1024, ge=1024)
    observe_unnamed_buffers: bool = False
    cursor_coalesce_ms: int = Field(default=250, ge=50, le=5000)
    max_queue_events: int = Field(default=2000, ge=100)


class RetentionSettings(_Model):
    raw_events_days: int = Field(default=7, ge=0)
    transcripts_days: int = Field(default=30, ge=0)


class Providers(_Model):
    anthropic: ProviderSettings = ProviderSettings()
    openai: ProviderSettings = ProviderSettings()
    mock: ProviderSettings = ProviderSettings(model="mock-tutor")


class Config(_Model):
    schema_version: int = 1
    initialized: bool = False
    workspace_root: str = DEFAULT_WORKSPACE
    extra_roots: list[str] = []
    tutor: TutorSettings = TutorSettings()
    providers: Providers = Providers()
    observation: ObservationSettings = ObservationSettings()
    retention: RetentionSettings = RetentionSettings()

    def provider_settings(self, name: ProviderName | None = None) -> ProviderSettings:
        result: ProviderSettings = getattr(self.providers, name or self.tutor.provider)
        return result

    def roots(self) -> list[Path]:
        """Approved observation roots, expanded and resolved."""
        raw = [self.workspace_root, *self.extra_roots]
        return [Path(r).expanduser().resolve() for r in raw if r]


HEADER = (
    "CLI Mentor configuration. Secrets do NOT belong here.\n"
    "API keys are read from the macOS Keychain or environment variables.\n"
    "Edit with `mentor init`, or by hand and check with `mentor doctor`."
)


def load_config(path: Path) -> Config:
    if not path.exists():
        return Config()
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return Config.model_validate(data)


def save_config(config: Config, path: Path) -> None:
    doc = tomlkit.document()
    for line in HEADER.splitlines():
        doc.add(tomlkit.comment(line))
    data = config.model_dump(mode="json", exclude_none=True)
    for key, value in data.items():
        doc[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    write_private_file(path, tomlkit.dumps(doc))
