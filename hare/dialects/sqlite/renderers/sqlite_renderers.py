from __future__ import annotations

import datetime
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from hare.dialects.base.renderers.checked_term_renderers import CheckedTermRenderers
from hare.dialects.sqlite.lookups.json.sqlite_json_type_criterion import SqliteJsonTypeCriterion
from hare.dialects.sqlite.renderers.sqlite_defaults import SqliteDefaults
from hare.dialects.sqlite.renderers.sqlite_json_renderers import SqliteJsonRenderers
from hare.dialects.sqlite.renderers.sqlite_number_renderers import SqliteNumberRenderers
from hare.dialects.sqlite.renderers.sqlite_temporal_renderers import SqliteTemporalRenderers
from hare.dialects.sqlite.renderers.sqlite_text_renderers import SqliteTextRenderers
from hare.exceptions import UnSupportedError
from hare.query.expressions.constants import DECIMAL_WITHOUT_TYPE_MESSAGE
from hare.sql.sql_context import SqlContext
from hare.sql.terms.values.row_value_list import RowValueList

if TYPE_CHECKING:
    from hare.sql.terms.term import Term


class SqliteRenderers(CheckedTermRenderers):
    """How SQLite renders the terms whose SQL differs between dialects - SQLite's own functions, and
    hare's for what SQLite has none of or a type hare keeps in a form of its own."""

    def add_dialect_renderers(self) -> None:
        SqliteJsonRenderers.register(self)
        self.register(SqliteJsonTypeCriterion, SqliteJsonRenderers.render_json_type)
        SqliteTemporalRenderers.register(self)
        SqliteNumberRenderers.register(self)
        SqliteTextRenderers.register(self)
        self.register(RowValueList, self.render_row_value_list)
        SqliteDefaults.register(self)
        # Local import: the vector, search and spatial terms import the SQL functions package, which
        # imports this one.
        from hare.dialects.sqlite.search.sqlite_search_renderers import SqliteSearchRenderers
        from hare.dialects.sqlite.spatial.sqlite_spatial_renderers import SqliteSpatialRenderers
        from hare.dialects.sqlite.vectors.sqlite_vector_renderers import SqliteVectorRenderers

        SqliteVectorRenderers.register(self)
        SqliteSearchRenderers.register(self)
        SqliteSpatialRenderers.register(self)

    @staticmethod
    def render_row_value_list(row_value_list: RowValueList, sql_context: SqlContext) -> str:
        """``(VALUES (?, ?), ...)`` - SQLite before 3.37 takes no list of row values after ``IN``."""
        rows_sql = ",".join(row.get_sql(sql_context) for row in row_value_list.values)
        return sql_context.format_alias_sql(f"(VALUES {rows_sql})", row_value_list.alias)

    is_distinct_from_operator = "IS NOT"

    def get_json_path_comparand(
        self, value: Any, encode_json_text: Callable[[Any], str], column_type: str | None, *, as_parameter: bool
    ) -> Any:
        # JSON is text read by functions: a number compares as-is and any other value as JSON
        # text, the form the path's own expression gives.
        if isinstance(value, Decimal):
            raise UnSupportedError(DECIMAL_WITHOUT_TYPE_MESSAGE.format(value=value, dialect=self.dialect.name))
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return self.dialect.parameters.get_bindable_number(value)
        return encode_json_text(value)

    def get_datetime_part_comparand(self, value: datetime.date | datetime.time) -> Any:
        # A datetime is stored as ISO text; its date or time of day is read as ISO text too.
        # Local import: the constants module instantiates this class.
        from hare.dialects.sqlite.constants import TIME_TEXT_FORMAT

        if isinstance(value, datetime.time):
            return TIME_TEXT_FORMAT.format(value)
        return value.isoformat()

    def get_assigned_integer_term(self, term: Term) -> Term:
        # An INTEGER column keeps a fraction as a REAL - converted as SQLite's own CAST converts. Local
        # import: hare.sql's terms render through the dialect.
        from hare.sql.functions.cast import Cast

        return Cast(term, "INTEGER")

    def get_composite_distinct_key(self, terms: Sequence[Term]) -> Term:
        # Local import: hare.sql's terms render through the dialect.
        from hare.sql.terms.functions.function import Function

        # A JSON array - SQLite has no row values in COUNT(DISTINCT ...).
        return Function("JSON_ARRAY", *terms)
