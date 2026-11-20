from __future__ import annotations

from collections.abc import Iterator

from hare.exceptions import UnSupportedError
from hare.sql.sql_context import SqlContext
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.node import Node
from hare.sql.terms.term import Term
from hare.sql.terms.term_identity_text import TermIdentityText


class JSONAttributeCriterion(Criterion):
    """A path into a JSON column - written by each dialect's renderer in its own form."""

    structure_attributes = ("as_text", "json_column", "path")

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

    def get_children(self) -> Iterator[Node]:
        yield self.json_column

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
