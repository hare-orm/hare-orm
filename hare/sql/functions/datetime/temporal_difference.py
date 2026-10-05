from __future__ import annotations

from typing import Any

from hare.sql.terms.functions.function import Function


class TemporalDifference(Function):
    """Difference `left - right` of two datetime or two date terms, as a BIGINT of microseconds.

    Postgres derives it from `EXTRACT(EPOCH ...)` (exact numeric on PostgreSQL 14+) for datetimes
    and from the integer day difference for dates; SQLite dispatches to a Python UDF.
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
