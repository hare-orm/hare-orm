from __future__ import annotations

from typing import Any

from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term


class Extract(Function):
    """A part of a date or time - written by the dialect's renderer."""

    structure_attributes = (
        "args",
        "as_integer",
        "date_part",
        "is_time_of_day",
        "use_local_zone_when_naive",
        "zone_as_literal",
        "zone_name",
    )
    has_repeatable_value = True

    requires_dialect_renderer = True

    def __init__(
        self,
        date_part: Any,
        term: Term,
        alias: str | None = None,
        *,
        zone_name: str | None = None,
        use_local_zone_when_naive: bool = False,
        as_integer: bool = False,
        zone_as_literal: bool = False,
        is_time_of_day: bool = False,
    ) -> None:
        """
        Args:
            date_part: Which component to extract (``DatePart.YEAR``, etc).
            term: The term to extract from.
            zone_name: When set, the extraction happens in this named time zone instead of
                whatever zone the underlying value is already stored/returned in. Only passed
                for an aware ``DatetimeField`` under ``use_timezone=True``; a ``DateField`` has no
                time component for a zone to shift, and passing ``None`` here (the default)
                renders exactly the previous, unqualified extraction.
            use_local_zone_when_naive: When ``zone_name`` is ``None`` and this is ``True``, the
                extraction reads the value in the machine's local zone - on a dialect whose
                timestamp column holds an instant (a naive value held as an instant in that zone,
                while a bare ``EXTRACT`` would read it in the session's zone). A dialect storing the
                wall-clock digits themselves extracts them as they are and ignores it. Only set for a
                naive ``DatetimeField`` (``use_timezone=False``).
            alias: Optional alias for the term.
            as_integer: Cast the result to an integer - ``EXTRACT`` may return a numeric.
            zone_as_literal: Render ``zone_name`` as a literal rather than a parameter - an
                extraction selected, grouped, ordered and made distinct by renders the same
                expression in each clause, which a database compares by text (two parameters
                are two expressions). The zone is a validated IANA name.
            is_time_of_day: The term is a time of day - on a database storing one as text, the text
                is read as a time first.
        """
        super().__init__("EXTRACT", date_part, term, alias=alias)
        self.date_part = date_part
        self.zone_name = zone_name
        self.use_local_zone_when_naive = use_local_zone_when_naive
        self.as_integer = as_integer
        self.zone_as_literal = zone_as_literal
        self.is_time_of_day = is_time_of_day

    @property
    def field(self) -> Term:
        """The extracted value, stored with the function's structural arguments."""
        return self.args[1]
