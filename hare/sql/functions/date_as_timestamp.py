from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql.terms.functions.function import Function

if TYPE_CHECKING:
    pass


class DateAsTimestamp(Function):
    """A date compared with a timestamp, as the first moment of its day - in ``zone_name`` for an aware
    timestamp; a naive one's wall-clock midnight (Postgres holds a naive value as an instant in the
    machine's local zone)."""

    requires_dialect_renderer = True

    def __init__(self, term: Any, zone_name: str | None, alias: str | None = None) -> None:
        """
        Args:
            term: The date.
            zone_name: The zone of an aware timestamp's day, None for a naive timestamp.
            alias: Optional alias for the term.
        """
        super().__init__("CAST", term, alias=alias)
        self.zone_name = zone_name
