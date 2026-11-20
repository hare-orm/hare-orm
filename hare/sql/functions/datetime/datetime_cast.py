from __future__ import annotations

from hare.sql.enums import DatetimeCastTarget
from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term


class DatetimeCast(Function):
    """A datetime's date or time of day, in a time zone - ``field__date``/``field__time``.

    The value is cast at the zone - by a cast of the database, or hare's date-part function returning
    the date as ``YYYY-MM-DD`` and the time as fixed-width ``HH:MM:SS.ffffff`` text.
    """

    structure_attributes = ("args", "target", "use_local_zone_when_naive", "zone_name")
    has_repeatable_value = True

    requires_dialect_renderer = True

    def __init__(
        self,
        target: DatetimeCastTarget,
        term: Term,
        alias: str | None = None,
        *,
        zone_name: str | None = None,
        use_local_zone_when_naive: bool = False,
    ) -> None:
        """
        Args:
            target: ``DatetimeCastTarget.DATE`` or ``DatetimeCastTarget.TIME``.
            term: The datetime term.
            zone_name: The zone to read the value in, for an aware datetime.
            use_local_zone_when_naive: Read a naive datetime (``use_timezone=False``) in the local
                system zone where the column holds an instant - see ``Extract``.
            alias: Optional alias for the term.
        """
        super().__init__("CAST", term, alias=alias)
        self.target = target
        self.zone_name = zone_name
        self.use_local_zone_when_naive = use_local_zone_when_naive

    @property
    def field(self) -> Term:
        """The source expression, stored as the function's first argument."""
        return self.args[0]
