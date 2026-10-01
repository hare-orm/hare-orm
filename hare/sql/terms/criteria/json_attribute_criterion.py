from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.terms.base.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    pass


from typing import TYPE_CHECKING

from hare.exceptions import UnSupportedError
from hare.sql.context import SqlContext
from hare.sql.utils import builder

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.queries.tables.table import Table
from hare.sql.terms.criteria.criterion import Criterion


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

    def get_sql(self, ctx: SqlContext) -> str:
        renderer = ctx.dialect.renderers.get(type(self))
        if renderer is None:
            raise UnSupportedError(f"JSON path access has no SQL for the {ctx.dialect} dialect")
        sql = renderer(self, ctx)
        if ctx.with_alias:
            return ctx.format_alias_sql(sql, self.alias)
        return sql

    @builder
    def replace_table(  # type:ignore[return]
        self, current_table: Table | None, new_table: Table | None
    ) -> Self:
        self.json_column = self.json_column.replace_table(current_table, new_table)


class JSONTypeCriterion(JSONAttributeCriterion):
    """The JSON type name of the value at a path, as SQLite's ``json_type()`` spells it
    (``null``, ``true``, ``false``, ``integer``, ``real``, ``text``, ``array``, ``object``), or
    NULL for a missing path. SQLite only."""

    def __init__(self, json_column: Term, path: list[str | int], alias: str | None = None) -> None:
        super().__init__(json_column, path, alias, as_text=False)
