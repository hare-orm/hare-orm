from __future__ import annotations

from typing import Any

from hare.sql.terms.functions.function import Function


class TemporalDifference(Function):
    """Difference `left - right` of two datetime or two date terms, as a BIGINT of microseconds.

    Each dialect computes it exactly - from the epoch difference for datetimes and the integer day
    difference for dates, or by hare's function where it has none.
    """

    requires_dialect_renderer = True

    def __init__(self, left: Any, right: Any, *, is_date: bool = False, alias: str | None = None) -> None:
        """
        Args:
            left: The minuend datetime/date term.
            right: The subtrahend datetime/date term.
            is_date: Whether both terms are dates rather than datetimes.
            alias: Optional alias for the term.
        """
        super().__init__("TEMPORAL_DIFFERENCE", left, right, alias=alias)
        self.is_date = is_date
