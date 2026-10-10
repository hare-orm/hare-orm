from __future__ import annotations

from typing import TYPE_CHECKING

from hare.exceptions import UnSupportedError
from hare.sql.builder_methods import BuilderMethods
from hare.sql.sql_context import SqlContext
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from typing import Self

    from hare.sql.builder.tables.table import Table


class JSONAttributeCriterion(Criterion):
    """A path into a JSON column - ``data->'user'->>'age'`` on PostgreSQL, ``data->>'$.user.age'`` on
    SQLite.
    """

    def __init__(
        self, json_column: Term, path: list[str | int], alias: str | None = None, *, as_text: bool = True
    ) -> None:
        """Initializes a JSON attribute access criterion.

        Args:
            json_column: The JSON column/field to access.
            path: The path to the attribute as a list of keys/indices.
            alias: Optional alias for the expression.
            as_text: Whether the final path segment extracts its value as text (Postgres
                ``->>``/SQLite unwrapped ``json_extract``) rather than as JSON (Postgres
                ``->``/``#>``; SQLite JSON text, a number staying a number).
        """
        super().__init__(alias)

        self.json_column = json_column
        self.path = path
        self.as_text = as_text

    def get_text_term(self) -> JSONAttributeCriterion:
        """The same path, extracting its value as text."""
        return JSONAttributeCriterion(self.json_column, self.path, self.alias, as_text=True)

    def get_sql(self, sql_context: SqlContext) -> str:
        renderer = sql_context.dialect.renderers.get(type(self))
        if renderer is None:
            raise UnSupportedError(f"JSON path access has no SQL for the {sql_context.dialect} dialect")
        sql = renderer(self, sql_context)
        if sql_context.with_alias:
            return sql_context.format_alias_sql(sql, self.alias)
        return sql

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        self.json_column = self.json_column.replace_table(current_table, new_table)
        return self
