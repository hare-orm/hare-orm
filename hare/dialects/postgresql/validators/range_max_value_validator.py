from __future__ import annotations

from typing import Any

from hare.dialects.postgresql.validators.range_bound_validator import RangeBoundValidator


class RangeMaxValueValidator(RangeBoundValidator):
    """The range's upper bound is at most ``limit_value`` - an unbounded upper side exceeds any
    limit; an empty range has no bounds and passes."""

    def __call__(self, value: Any) -> None:
        value_range = self.get_range(value)
        if value_range.is_empty:
            return
        if value_range.upper is None or value_range.upper > self.limit_value:
            self._raise(f"Ensure that the upper bound of the range is not greater than {self.limit_value}")
