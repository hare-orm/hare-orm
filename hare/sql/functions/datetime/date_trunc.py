from __future__ import annotations

from hare.sql.enums import DateTruncSource, TruncType
from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term


class DateTrunc(Function):
    """A date or time truncated to a ``TruncType`` - a datetime in a zone; a datetime's
    ``date``/``time`` gives a date or a time of day. Each dialect renders it its own way.
    """

    structure_attributes = ("args", "source", "trunc_type", "zone_name")
    has_repeatable_value = True

    requires_dialect_renderer = True

    def __init__(
        self,
        trunc_type: TruncType,
        term: Term,
        source: DateTruncSource,
        alias: str | None = None,
        *,
        zone_name: str | None = None,
    ) -> None:
        """
        Args:
            trunc_type: What to keep.
            term: The date/time term.
            source: The type of value ``term`` holds.
            alias: Optional alias for the term.
            zone_name: The zone a datetime is truncated in.
        """
        super().__init__("TRUNC_DATETIME", term, alias=alias)
        self.trunc_type = trunc_type
        self.source = source
        self.zone_name = zone_name

    @property
    def field(self) -> Term:
        """The source expression, stored as the function's first argument."""
        return self.args[0]
