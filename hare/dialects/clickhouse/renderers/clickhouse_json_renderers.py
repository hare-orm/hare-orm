from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from hare.dialects.clickhouse.lookups.constants import CLICKHOUSE_JSON_SORT_KEY_SQL, CLICKHOUSE_JSON_VALUE_TEXT_SQL
from hare.dialects.clickhouse.renderers.clickhouse_json_texts import ClickhouseJsonTexts
from hare.dialects.clickhouse.renderers.constants import CLICKHOUSE_JSON_AS_TEXT_TEMPLATE
from hare.exceptions import UnSupportedError
from hare.sql import functions
from hare.sql.sql_context import SqlContext
from hare.sql.terms import criteria

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.sql.functions.json.json_value import JsonValue
    from hare.sql.terms.values.value_wrapper import ValueWrapper


class ClickhouseJsonRenderers:
    """How ClickHouse reads JSON stored as text: a value at a path as JSON text or as plain text,
    compared as JSON text; an object or an array is built as JSON text."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the json renderers on ClickHouse's renderers.

        Args:
            renderers: The renderers.
        """
        renderers.register(functions.JsonObject, cls.render_json_object)
        renderers.register(functions.JsonArray, cls.render_json_array)
        renderers.register(functions.JsonValue, cls.render_json_value)
        renderers.register(functions.JsonComparand, cls.render_json_comparand)
        renderers.register(functions.JsonSortKey, cls.render_json_sort_key)
        renderers.register(functions.JsonAsText, cls.render_json_as_text)
        renderers.register(criteria.JSONAttributeCriterion, cls.render_json_attribute)
        renderers.register(criteria.JSONTypeCriterion, cls.render_json_type)

    @staticmethod
    def render_json_as_text(function: functions.JsonAsText, sql_context: SqlContext) -> str:
        return CLICKHOUSE_JSON_AS_TEXT_TEMPLATE.format(function.get_arg_sql(function.args[0], sql_context))

    @staticmethod
    def render_json_object(function: functions.JsonObject, sql_context: SqlContext) -> str:
        """The object's JSON text - each key a JSON string, each value its JSON text, in order."""
        parts_sql = []
        for position, (key, value) in enumerate(zip(function.args[::2], function.args[1::2], strict=True)):
            key_text = ("," if position else "{") + json.dumps(cast("ValueWrapper", key).value) + ":"
            parts_sql += [
                sql_context.dialect.literals.get_string_literal_sql(key_text),
                ClickhouseJsonTexts.get_json_text_sql(cast("JsonValue", value), sql_context),
            ]
        if not parts_sql:
            return "'{}'"
        return f"concat({', '.join(parts_sql)}, '}}')"

    @staticmethod
    def render_json_array(function: functions.JsonArray, sql_context: SqlContext) -> str:
        """The array's JSON text - each value its JSON text, in order."""
        values_sql = [
            ClickhouseJsonTexts.get_json_text_sql(cast("JsonValue", value), sql_context) for value in function.args
        ]
        if not values_sql:
            return "'[]'"
        separated_sql = ", ',', ".join(values_sql)
        return f"concat('[', {separated_sql}, ']')"

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
        own text and any other value's JSON text, NULL for a missing path and a JSON null alike. A
        digit of the path indexes an array - from 1 in ClickHouse's path, from the end when negative -
        and names a key of an object."""
        column_sql = criterion.json_column.get_sql(sql_context)
        if not criterion.path:
            return column_sql
        # The JSON text - of a JSON column, or of a String one holding it.
        column_sql = f"toString({column_sql})"
        literals = sql_context.dialect.literals
        if any(isinstance(part, int) for part in criterion.path):
            # A digit is an index of an array and a key of an object, as PostgreSQL reads it - each
            # step taken on the value the steps before it reached.
            value_sql = column_sql
            for part in criterion.path:
                if isinstance(part, int):
                    index_sql = str(part + 1 if part >= 0 else part)
                    key_sql = literals.get_string_literal_sql(str(part))
                    value_sql = (
                        f"if(JSONType({value_sql}) = 'Array', JSONExtractRaw({value_sql}, {index_sql}), "
                        f"JSONExtractRaw({value_sql}, {key_sql}))"
                    )
                else:
                    value_sql = f"JSONExtractRaw({value_sql}, {literals.get_string_literal_sql(part)})"
            raw_sql = f"nullIf({value_sql}, '')"
            if not criterion.as_text:
                return raw_sql
            type_sql = f"JSONType({value_sql})"
            return (
                f"multiIf({type_sql} = 'Null', NULL, {type_sql} = 'String', JSONExtractString({value_sql}), {raw_sql})"
            )
        path_sql = ", ".join(literals.get_string_literal_sql(str(part)) for part in criterion.path)
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
