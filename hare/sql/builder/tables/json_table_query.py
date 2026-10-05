from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any, Self

from hare.sql.builder.tables.aliased_query import AliasedQuery
from hare.sql.builder_methods import BuilderMethods
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions.joins.json_table_column import JsonTableColumn
    from hare.sql.builder.tables.table import Table
    from hare.sql.sql_context import SqlContext
    from hare.sql.terms.node import TNode
    from hare.sql.terms.term import Term


class JsonTableQuery(AliasedQuery):
    """The rows a JSON document's items make, joined in ``FROM`` under ``name`` - each dialect writes
    its ``JSON_TABLE``; equal to another of the same name, so it is joined once.

    Args:
        name: The name it is joined under.
        document: The document - a column of the tables before it, or an expression over them.
        path: The JSON path of the items.
        columns: The columns read from each item.
        ordinality_name: The column numbering the items from 1, None for none.
    """

    def __init__(
        self,
        name: str,
        document: Term,
        path: str,
        columns: Sequence[JsonTableColumn],
        ordinality_name: str | None,
    ) -> None:
        super().__init__(name)
        self.document = document
        self.path = path
        self.columns = tuple(columns)
        self.ordinality_name = ordinality_name

    def nodes_(self) -> Iterator[TNode]:
        yield from super().nodes_()
        yield from self.document.nodes_()

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        self.document = self.document.replace_table(current_table, new_table)
        return self

    def get_sql(self, sql_context: SqlContext) -> str:
        literal_context = sql_context.copy(with_alias=False)
        document_sql = self.document.get_sql(sql_context.copy(with_alias=False, with_namespace=True))
        columns_sql = [
            (
                sql_context.quote(column.name),
                column.field.get_column_type(sql_context.dialect),
                ValueWrapper(column.path, allow_parametrize=False).get_sql(literal_context),
            )
            for column in self.columns
        ]
        json_table_sql = sql_context.dialect.clauses.get_json_table_sql(
            document_sql,
            ValueWrapper(self.path, allow_parametrize=False).get_sql(literal_context),
            columns_sql,
            None if self.ordinality_name is None else sql_context.quote(self.ordinality_name),
        )
        return sql_context.format_alias_sql(json_table_sql, self.alias)

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, JsonTableQuery) and self.name == other.name

    def __hash__(self) -> int:
        return hash(str(self.name))
