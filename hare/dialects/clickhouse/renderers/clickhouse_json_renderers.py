from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.clickhouse.lookups.constants import CLICKHOUSE_JSON_SORT_KEY_SQL, CLICKHOUSE_JSON_VALUE_TEXT_SQL
from hare.exceptions import UnSupportedError
from hare.sql import functions
from hare.sql.sql_context import SqlContext
from hare.sql.terms import criteria

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers


class ClickhouseJsonRenderers:
    """How ClickHouse reads JSON stored as text: a value at a path as JSON text or as plain text,
    compared as JSON text. Objects and arrays aren't built - a ClickHouse map or tuple holds values
    of fixed types."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the json renderers on ClickHouse's renderers.

        Args:
            renderers: The renderers.
        """
        renderers.register(functions.JsonObject, cls.render_json_builder)
        renderers.register(functions.JsonArray, cls.render_json_builder)
        renderers.register(functions.JsonValue, cls.render_json_value)
        renderers.register(functions.JsonComparand, cls.render_json_comparand)
        renderers.register(functions.JsonSortKey, cls.render_json_sort_key)
        renderers.register(criteria.JSONAttributeCriterion, cls.render_json_attribute)
        renderers.register(criteria.JSONTypeCriterion, cls.render_json_type)

    @staticmethod
    def render_json_builder(function: functions.JsonObject | functions.JsonArray, sql_context: SqlContext) -> str:
        raise UnSupportedError("ClickHouse builds no JSON object or array of values of any types")

    @staticmethod
    def render_json_value(function: functions.JsonValue | functions.JsonSortKey, sql_context: SqlContext) -> str:
        return function.get_arg_sql(function.args[0], sql_context)

    @staticmethod
    def render_json_comparand(function: functions.JsonComparand, sql_context: SqlContext) -> str:
        """The value as JSON text, re-written as a path reads it - ``toJSONString()`` escapes a slash."""
        return CLICKHOUSE_JSON_VALUE_TEXT_SQL.format(
            value=f"toJSONString({function.get_arg_sql(function.args[0], sql_context)})"
        )

    @staticmethod
    def render_json_sort_key(function: functions.JsonSortKey, sql_context: SqlContext) -> str:
        """The JSON value ordered as PostgreSQL orders ``jsonb`` - not as text, where ``10`` is before ``9``."""
        return CLICKHOUSE_JSON_SORT_KEY_SQL.format(value=function.get_arg_sql(function.args[0], sql_context))

    @staticmethod
    def render_json_attribute(criterion: criteria.JSONAttributeCriterion, sql_context: SqlContext) -> str:
        """The value at a path: its JSON text as ClickHouse writes it - NULL for a missing path, the
        text ``null`` for a JSON null, as PostgreSQL's ``->`` reads it - or with ``as_text`` a string's
        own text and any other value's JSON text, NULL for a missing path and a JSON null alike. An
        array index counts from 1 in ClickHouse's path, from the end when negative."""
        column_sql = criterion.json_column.get_sql(sql_context)
        if not criterion.path:
            return column_sql
        # The JSON text - of a JSON column, or of a String one holding it.
        column_sql = f"toString({column_sql})"
        path_sql = ", ".join(
            str(part + 1 if part >= 0 else part)
            if isinstance(part, int)
            else sql_context.dialect.literals.get_string_literal_sql(part)
            for part in criterion.path
        )
        raw_sql = f"nullIf(JSONExtractRaw({column_sql}, {path_sql}), '')"
        if not criterion.as_text:
            return raw_sql
        type_sql = f"JSONType({column_sql}, {path_sql})"
        return (
            f"multiIf({type_sql} = 'Null', NULL, {type_sql} = 'String', "
            f"JSONExtractString({column_sql}, {path_sql}), {raw_sql})"
        )

    @staticmethod
    def render_json_type(criterion: criteria.JSONTypeCriterion, sql_context: SqlContext) -> str:
        raise UnSupportedError("The JSON type name of a path is SQLite's json_type() - ClickHouse has no such text")
