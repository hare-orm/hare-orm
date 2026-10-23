from __future__ import annotations

from typing import Any

from hare.sql.terms.functions.function import Function


class TemporalShift(Function):
    """Adds a signed microsecond count to a datetime or date - an absolute duration, never a calendar
    day; a date result is floored back to a date. A dialect writes it as interval arithmetic of whole
    hours plus a microsecond remainder, or calls hare's function doing the same on the stored text.
    """

    requires_dialect_renderer = True

    def __init__(
        self,
        base: Any,
        microseconds: Any,
        *,
        is_date: bool = False,
        is_subtraction: bool = False,
        is_naive: bool = False,
        alias: str | None = None,
    ) -> None:
        """
        Args:
            base: The datetime/date term to shift.
            microseconds: A BIGINT term holding the (unsigned-by-operator) microsecond count.
            is_date: Whether `base` is a date rather than a datetime.
            is_subtraction: Whether the count is subtracted instead of added.
            is_naive: Whether `base` is a wall clock in no zone (``NaiveDatetimeField``).
            alias: Optional alias for the term.
        """
        super().__init__("TEMPORAL_SHIFT", base, microseconds, alias=alias)
        self.is_date = is_date
        self.is_subtraction = is_subtraction
        self.is_naive = is_naive
