from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql.terms.functions.function import Function

if TYPE_CHECKING:
    pass


class TemporalShift(Function):
    """Adds a signed microsecond count to a datetime or date. Postgres renders ``interval`` arithmetic
    of whole hours plus a microsecond remainder - an absolute duration, never a calendar day; a date
    result is floored back to a date. SQLite calls a UDF doing the same on the stored ISO text.
    """

    requires_dialect_renderer = True

    def __init__(
        self,
        base: Any,
        microseconds: Any,
        *,
        is_date: bool = False,
        is_subtraction: bool = False,
        alias: str | None = None,
    ) -> None:
        """
        Args:
            base: The datetime/date term to shift.
            microseconds: A BIGINT term holding the (unsigned-by-operator) microsecond count.
            is_date: Whether `base` is a date rather than a datetime.
            is_subtraction: Whether the count is subtracted instead of added.
            alias: Optional alias for the term.
        """
        super().__init__("TEMPORAL_SHIFT", base, microseconds, alias=alias)
        self.is_date = is_date
        self.is_subtraction = is_subtraction
