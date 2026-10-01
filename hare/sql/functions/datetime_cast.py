from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.enums import DatetimeCastTarget
from hare.sql.terms.base.term import Term
from hare.sql.terms.functions.function import Function
from hare.sql.utils import builder

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Self

    from hare.sql.queries.tables.table import Table
    from hare.sql.terms.base.node import TNode


class DatetimeCast(Function):
    """A datetime's date or time of day, in a time zone - ``field__date``/``field__time``.

    Postgres casts the value at the zone; SQLite calls the date-part UDF, which returns the date
    as ``YYYY-MM-DD`` and the time as fixed-width ``HH:MM:SS.ffffff`` text.
    """

    requires_dialect_renderer = True

    def __init__(
        self,
        target: DatetimeCastTarget,
        field: Term,
        alias: str | None = None,
        *,
        zone_name: str | None = None,
        use_local_zone_when_naive: bool = False,
    ) -> None:
        """
        Args:
            target: ``DatetimeCastTarget.DATE`` or ``DatetimeCastTarget.TIME``.
            field: The datetime term.
            zone_name: The zone to read the value in, for an aware datetime.
            use_local_zone_when_naive: Read a naive datetime (``use_tz=False``) in the local
                system zone on Postgres - see ``Extract``.
            alias: Optional alias for the term.
        """
        super().__init__("CAST", field, alias=alias)
        self.target = target
        self.field = field
        self.zone_name = zone_name
        self.use_local_zone_when_naive = use_local_zone_when_naive

    def nodes_(self) -> Iterator[TNode]:
        yield from super().nodes_()
        yield from self.field.nodes_()

    @builder
    def replace_table(  # type:ignore[return]
        self, current_table: Table | None, new_table: Table | None
    ) -> Self:
        """Replaces all occurrences of the specified table with the new table.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the term with the tables replaced.
        """
        self.args = [param.replace_table(current_table, new_table) for param in self.args]
        self.field = self.field.replace_table(current_table, new_table)
