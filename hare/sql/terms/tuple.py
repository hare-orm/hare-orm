from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from hare.sql.builder_methods import BuilderMethods
from hare.sql.sql_context import SqlContext
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.node import TNode
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from typing import Self

    from hare.sql.builder.tables.table import Table


class Tuple(Criterion):
    def __init__(self, *values: Any) -> None:
        super().__init__()
        self.values = [self.wrap_constant(value) for value in values]

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        for value in self.values:
            yield from value.nodes_()

    def get_sql(self, sql_context: SqlContext) -> str:
        sql = "({})".format(",".join(term.get_sql(sql_context) for term in self.values))
        return sql_context.format_alias_sql(sql, self.alias)

    @property
    def is_aggregate(self) -> bool | None:  # type:ignore[override]
        return Term.get_combined_is_aggregate([value.is_aggregate for value in self.values])

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        """Replaces all occurrences of the specified table with the new table.

        Useful when reusing fields across queries.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the field with the tables replaced.
        """
        self.values = [value.replace_table(current_table, new_table) for value in self.values]
        return self
