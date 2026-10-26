from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from hare.exceptions import UnSupportedError
from hare.sql.builder_methods import BuilderMethods
from hare.sql.sql_context import SqlContext
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.node import TNode
from hare.sql.terms.term import Term
from hare.sql.terms.term_identity_text import TermIdentityText

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.builder.tables.table import Table


class JSONAttributeCriterion(Criterion):
    """A path into a JSON column - written by each dialect's renderer in its own form."""

    def __init__(
        self,
        json_column: Term,
        path: list[str | int],
        alias: str | None = None,
        *,
        as_text: bool = True,
    ) -> None:
        """Initializes a JSON attribute access criterion.

        Args:
            json_column: The JSON column/field to access.
            path: The path to the attribute as a list of keys/indices.
            alias: Optional alias for the expression.
            as_text: Whether the final path segment extracts its value as text rather than as JSON.
        """
        super().__init__(alias)

        self.json_column = json_column
        self.path = path
        self.as_text = as_text

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        yield from self.json_column.nodes_()

    def get_text_term(self) -> JSONAttributeCriterion:
        """The same path, extracting its value as the text a text lookup matches."""
        return JSONAttributeCriterion(self.json_column, self.path, self.alias, as_text=True)

    def get_sql(self, sql_context: SqlContext) -> str:
        renderer = sql_context.dialect.renderers.get(type(self))
        if renderer is not None:
            sql = renderer(self, sql_context)
        elif sql_context.names_dialect_terms:
            sql = TermIdentityText.get_sql(self, sql_context)
        else:
            raise UnSupportedError(f"JSON path access has no SQL for the {sql_context.dialect} dialect")
        if sql_context.with_alias:
            return sql_context.format_alias_sql(sql, self.alias)
        return sql

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        self.json_column = self.json_column.replace_table(current_table, new_table)
        return self
