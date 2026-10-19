"""Per-source ordering checks.

Each source numbers its events 1, 2, 3, ... inside an *epoch* (one run of that
source). We can detect duplicates, gaps, and restarts per source. We do not
claim a true global order across independent processes.
"""

from __future__ import annotations

from dataclasses import dataclass

from .schema import SourceRef


@dataclass(frozen=True)
class SeqVerdict:
    accept: bool
    duplicate: bool = False
    missing: int = 0  # how many events were skipped (gap size)
    new_epoch: bool = False

    @property
    def gap(self) -> bool:
        return self.missing > 0


class Sequencer:
    def __init__(self) -> None:
        self._last: dict[tuple[str, str], tuple[str, int]] = {}

    def check(self, source: SourceRef, seq: int) -> SeqVerdict:
        key = (source.kind.value, source.id)
        previous = self._last.get(key)
        if previous is None or previous[0] != source.epoch:
            self._last[key] = (source.epoch, seq)
            return SeqVerdict(accept=True, new_epoch=True, missing=max(0, seq - 1))
        last = previous[1]
        if seq <= last:
            return SeqVerdict(accept=False, duplicate=True)
        self._last[key] = (source.epoch, seq)
        return SeqVerdict(accept=True, missing=seq - last - 1)

    def last_seq(self, source: SourceRef) -> int | None:
        previous = self._last.get((source.kind.value, source.id))
        if previous is None or previous[0] != source.epoch:
            return None
        return previous[1]

    def forget(self, source: SourceRef) -> None:
        self._last.pop((source.kind.value, source.id), None)
