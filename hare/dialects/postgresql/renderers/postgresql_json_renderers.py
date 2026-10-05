from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.postgresql.renderers.postgresql_temporal_renderers import PostgresqlTemporalRenderers
from hare.exceptions import UnSupportedError
from hare.sql import functions
from hare.sql.enums import JsonValueType
from hare.sql.sql_context import SqlContext
from hare.sql.terms import criteria

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers


class PostgresqlJsonRenderers:
    """How PostgreSQL writes JSON: objects and arrays built, a value read at a path and compared, a
    sort key of a JSON value, and the tests of a key's presence and a value's type."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the json renderers on PostgreSQL's renderers.

        Args:
            renderers: The renderers.
        """
        renderers.register(functions.JsonObject, cls.render_json_object)
        renderers.register(functions.JsonArray, cls.render_json_array)
        renderers.register(functions.JsonValue, cls.render_json_value)
        renderers.register(functions.JsonComparand, cls.render_json_comparand)
        renderers.register(functions.JsonSortKey, cls.render_json_sort_key)
        renderers.register(criteria.JSONAttributeCriterion, cls.render_json_attribute)
        renderers.register(criteria.JSONTypeCriterion, cls.render_json_type)

    @staticmethod
    def render_json_object(function: functions.JsonObject, sql_context: SqlContext) -> str:
        arguments_sql = ",".join(function.get_arg_sql(argument, sql_context) for argument in function.args)
        return f"JSONB_BUILD_OBJECT({arguments_sql})"

    @staticmethod
    def render_json_array(function: functions.JsonArray, sql_context: SqlContext) -> str:
        return (
            f"JSONB_BUILD_ARRAY({','.join(function.get_arg_sql(argument, sql_context) for argument in function.args)})"
        )

    @classmethod
    def render_json_value(cls, function: functions.JsonValue, sql_context: SqlContext) -> str:
        """The value as it is - a naive timestamp as its wall clock."""
        term_sql = function.get_arg_sql(function.args[0], sql_context)
        if function.value_type != JsonValueType.DATETIME or function.is_aware:
            return term_sql
        zone_sql = PostgresqlTemporalRenderers.get_local_zone_sql(sql_context)
        return f"(CAST({term_sql} AS TIMESTAMPTZ) AT TIME ZONE {zone_sql})"

    @classmethod
    def render_json_comparand(cls, function: functions.JsonComparand, sql_context: SqlContext) -> str:
        return f"to_jsonb({cls.render_json_value(function, sql_context)})"

    @staticmethod
    def render_json_sort_key(function: functions.JsonSortKey, sql_context: SqlContext) -> str:
        """The value itself - jsonb orders its values."""
        return function.get_arg_sql(function.args[0], sql_context)

    @staticmethod
    def render_json_attribute(criterion: criteria.JSONAttributeCriterion, sql_context: SqlContext) -> str:
        """``data->'user'->>'age'``; a digit segment ``#>``/``#>>`` with a one-element text path,
        which reads an object's key or an array's index, whichever the value there is."""
        sql = criterion.json_column.get_sql(sql_context)
        for position, part in enumerate(criterion.path):
            is_last = position == len(criterion.path) - 1
            if isinstance(part, int):
                sql += f"{'#>>' if is_last and criterion.as_text else '#>'}'{{{part}}}'"
            else:
                operator = "->>" if is_last and criterion.as_text else "->"
                # A literal, not a parameter, so the expression matches an index on the same path.
                sql += f"{operator}{sql_context.dialect.literals.get_string_literal_sql(part)}"
        return sql

    @staticmethod
    def render_json_type(criterion: criteria.JSONTypeCriterion, sql_context: SqlContext) -> str:
        raise UnSupportedError("The JSON type name of a path is SQLite's json_type() - PostgreSQL has no such text")
