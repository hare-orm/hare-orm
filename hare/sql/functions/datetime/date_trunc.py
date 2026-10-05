from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.builder_methods import BuilderMethods
from hare.sql.enums import DateTruncSource, TruncType
from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Self

    from hare.sql.builder.tables.table import Table
    from hare.sql.terms.node import TNode


class DateTrunc(Function):
    """A date or time truncated to a ``TruncType`` - a datetime in a zone; a datetime's
    ``date``/``time`` gives a date or a time of day. Each dialect renders it its own way.
    """

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
        self.field = term
        self.source = source
        self.zone_name = zone_name

    def nodes_(self) -> Iterator[TNode]:
        yield from super().nodes_()
        yield from self.field.nodes_()

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        """Replaces all occurrences of the specified table with the new table.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the term with the tables replaced.
        """
        self.args = [parameter.replace_table(current_table, new_table) for parameter in self.args]
        self.field = self.field.replace_table(current_table, new_table)
        return self
