from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.enums import DateTruncSource, TruncType
from hare.sql.terms.base.term import Term
from hare.sql.terms.functions.function import Function
from hare.sql.utils import builder

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Self

    from hare.sql.queries.tables.table import Table
    from hare.sql.terms.base.node import TNode


class DateTrunc(Function):
    """A date or time truncated to a ``TruncType`` - a datetime in a zone; a datetime's
    ``date``/``time`` gives a date or a time of day. Postgres uses ``DATE_TRUNC`` (a time of day as
    its interval since midnight, keeping a ``TIMETZ`` offset); SQLite calls a UDF returning the
    stored text.
    """

    requires_dialect_renderer = True

    def __init__(
        self,
        trunc_type: TruncType,
        field: Term,
        source: DateTruncSource,
        alias: str | None = None,
        *,
        zone_name: str | None = None,
    ) -> None:
        """
        Args:
            trunc_type: What to keep.
            field: The date/time term.
            source: The type of value ``field`` holds.
            alias: Optional alias for the term.
            zone_name: The zone a datetime is truncated in.
        """
        super().__init__("DATE_TRUNC", field, alias=alias)
        self.trunc_type = trunc_type
        self.field = field
        self.source = source
        self.zone_name = zone_name

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
