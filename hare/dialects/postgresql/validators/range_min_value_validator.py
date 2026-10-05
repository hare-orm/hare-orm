from __future__ import annotations

from typing import Any

from hare.dialects.postgresql.validators.range_bound_validator import RangeBoundValidator


class RangeMinValueValidator(RangeBoundValidator):
    """The range's lower bound is at least ``limit_value`` - an unbounded lower side is below any
    limit; an empty range has no bounds and passes."""

    def __call__(self, value: Any) -> None:
        value_range = self.get_range(value)
        if value_range.is_empty:
            return
        if value_range.lower is None or value_range.lower < self.limit_value:
            self._raise(f"Ensure that the lower bound of the range is not less than {self.limit_value}")
