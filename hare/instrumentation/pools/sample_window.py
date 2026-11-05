from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SampleWindow:
    """Bounded observations between sequence cursors, including overwritten measurements.

    The interval is (start_cursor, end_cursor]. Values contain only the retained suffix.
    observations counts all measurements in that interval; missed counts those overwritten.
    An empty interval is distinct from an incomplete interval containing no usable samples.
    """

    start_cursor: int
    end_cursor: int
    values: tuple[float, ...]
    missed: int

    @property
    def observations(self) -> int:
        return max(0, self.end_cursor - self.start_cursor)

    @property
    def complete(self) -> bool:
        return self.missed == 0

    @classmethod
    def from_records(cls, cursor: int, records: tuple[int, list[float], int]) -> SampleWindow:
        """Adapt the shared Python/native ring-buffer result without discarding overflow."""
        end_cursor, values, missed = records
        return cls(cursor, end_cursor, tuple(values), missed)
