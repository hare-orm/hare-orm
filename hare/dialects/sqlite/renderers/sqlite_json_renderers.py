from __future__ import annotations

import json
from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.literals.constants import SQL_NULL_BYTE, SQL_NULL_BYTE_MESSAGE
from hare.dialects.sqlite.constants import (
    SQLITE_JSON_BYTES_FUNCTION_NAME,
    SQLITE_JSON_FLOAT_FUNCTION_NAME,
    SQLITE_JSON_PATH_FUNCTION_NAME,
    SQLITE_JSON_SORT_KEY_FUNCTION_NAME,
    SQLITE_JSON_TIME_FUNCTION_NAME,
    SQLITE_JSON_TIMESTAMP_FUNCTION_NAME,
)
from hare.exceptions import ValidationError
from hare.sql import functions
from hare.sql.enums import JsonValueType
from hare.sql.sql_context import SqlContext
from hare.sql.terms import criteria

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.sql.sql_context import SqlContext


class SqliteJsonRenderers:
    """How SQLite writes JSON: objects and arrays built, a value read at a path and compared, a sort
    key of a JSON value, and the tests of a key's presence and a value's type."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the json renderers on SQLite's renderers.

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

    #: The function writing each type of JSON value that needs one.
    JSON_VALUE_FUNCTION_NAMES: ClassVar[dict[JsonValueType, str]] = {
        JsonValueType.FLOAT: SQLITE_JSON_FLOAT_FUNCTION_NAME,
        JsonValueType.DATETIME: SQLITE_JSON_TIMESTAMP_FUNCTION_NAME,
        JsonValueType.TIME: SQLITE_JSON_TIME_FUNCTION_NAME,
        JsonValueType.BINARY: SQLITE_JSON_BYTES_FUNCTION_NAME,
    }

    @staticmethod
    def render_json_object(function: functions.JsonObject, sql_context: SqlContext) -> str:
        return f"json_object({','.join(function.get_arg_sql(argument, sql_context) for argument in function.args)})"

    @staticmethod
    def render_json_array(function: functions.JsonArray, sql_context: SqlContext) -> str:
        return f"json_array({','.join(function.get_arg_sql(argument, sql_context) for argument in function.args)})"

    @classmethod
    def render_json_value(cls, function: functions.JsonValue, sql_context: SqlContext) -> str:
        """A value written as PostgreSQL's jsonb holds it - a boolean as ``true``/``false``, a
        Decimal as a number, a timestamp as ISO text in UTC, a JSON value nested."""
        term_sql = function.get_arg_sql(function.args[0], sql_context)
        function_names = cls.JSON_VALUE_FUNCTION_NAMES
        if function.value_type == JsonValueType.DATETIME and not function.is_aware:
            return f"{function_names[function.value_type]}({term_sql},0)"
        if function.value_type == JsonValueType.BOOLEAN:
            return f"json(CASE {term_sql} WHEN 1 THEN 'true' WHEN 0 THEN 'false' END)"
        if function.value_type in {JsonValueType.DECIMAL, JsonValueType.JSON}:
            return f"json({term_sql})"
        if function.value_type == JsonValueType.FLOAT:
            return f"json({function_names[function.value_type]}({term_sql}))"
        if function.value_type == JsonValueType.DATETIME:
            return f"{function_names[function.value_type]}({term_sql},1)"
        if function.value_type in function_names:
            return f"{function_names[function.value_type]}({term_sql})"
        return term_sql

    @classmethod
    def render_json_comparand(cls, function: functions.JsonComparand, sql_context: SqlContext) -> str:
        """A number as it is and any other value as JSON text."""
        term_sql = function.get_arg_sql(function.args[0], sql_context)
        if function.value_type == JsonValueType.DECIMAL:
            return f"CAST({term_sql} AS NUMERIC)"
        if function.value_type == JsonValueType.FLOAT:
            return term_sql
        if function.value_type == JsonValueType.PLAIN:
            return (
                f"CASE WHEN typeof({term_sql}) IN ('integer','real','null') THEN {term_sql} "
                f"ELSE json_quote({term_sql}) END"
            )
        value_sql = cls.render_json_value(function, sql_context)
        return f"CASE WHEN {term_sql} IS NULL THEN NULL ELSE json_quote({value_sql}) END"

    @staticmethod
    def render_json_sort_key(function: functions.JsonSortKey, sql_context: SqlContext) -> str:
        """A byte key ordering JSON values as PostgreSQL orders jsonb."""
        term_sql = function.get_arg_sql(function.args[0], sql_context)
        return f"{SQLITE_JSON_SORT_KEY_FUNCTION_NAME}({term_sql})"

    @classmethod
    def render_json_attribute(cls, criterion: criteria.JSONAttributeCriterion, sql_context: SqlContext) -> str:
        """``json_extract()`` - SQLite's ``->>``/``->`` operators need 3.38.0. The value as JSON is
        JSON text, except a number, which stays an SQL number so it compares and orders
        numerically."""
        column_sql = criterion.json_column.get_sql(sql_context)
        if any(isinstance(part, int) for part in criterion.path):
            return cls.get_json_path_function_sql(column_sql, criterion.path, "text" if criterion.as_text else "json")
        path_sql = cls.get_json_native_path_sql(criterion.path)
        extract_sql = f"json_extract({column_sql}, {path_sql})"
        if criterion.as_text:
            return extract_sql
        return (
            f"CASE json_type({column_sql}, {path_sql}) WHEN 'true' THEN 'true' WHEN 'false' THEN 'false' "
            f"WHEN 'null' THEN 'null' WHEN 'text' THEN json_quote({extract_sql}) ELSE {extract_sql} END"
        )

    @classmethod
    def render_json_type(cls, criterion: criteria.JSONTypeCriterion, sql_context: SqlContext) -> str:
        column_sql = criterion.json_column.get_sql(sql_context)
        if any(isinstance(part, int) for part in criterion.path):
            return cls.get_json_path_function_sql(column_sql, criterion.path, "type")
        return f"json_type({column_sql}, {cls.get_json_native_path_sql(criterion.path)})"

    @staticmethod
    def get_json_path_function_sql(column_sql: str, path: list[str | int], mode: str) -> str:
        """A path with a digit segment, walked by hare's function - a digit segment is an object's
        key or an array's index, whichever the value at that point is, which no SQLite JSON path says.

        Args:
            column_sql: The JSON column.
            path: The path.
            mode: What the function returns: ``text`` (the unwrapped value), ``json`` or ``type``.

        Raises:
            ValidationError: The path contains a null byte.
        """
        path_json = json.dumps(path, ensure_ascii=False)
        if SQL_NULL_BYTE in path_json:
            raise ValidationError(SQL_NULL_BYTE_MESSAGE.format(text=path_json))
        path_sql = SqliteJsonRenderers.get_json_path_literal(path_json)
        return f"{SQLITE_JSON_PATH_FUNCTION_NAME}({column_sql}, {path_sql}, '{mode}')"

    @staticmethod
    def get_json_path_literal(path_text: str) -> str:
        """The SQL string literal of a SQLite JSON path."""
        return "'" + path_text.replace("'", "''") + "'"

    @staticmethod
    def get_json_native_path_sql(path: list[str | int]) -> str:
        """The literal of a SQLite JSON path of object keys - each key double-quoted, so a key with
        a dot in its name isn't read as two nested keys.

        Raises:
            ValidationError: A key contains a null byte.
        """
        segments = []
        for part in path:
            if SQL_NULL_BYTE in str(part):
                raise ValidationError(SQL_NULL_BYTE_MESSAGE.format(text=part))
            escaped = str(part).replace("\\", "\\\\").replace('"', '\\"')
            segments.append(f'."{escaped}"')
        return SqliteJsonRenderers.get_json_path_literal("$" + "".join(segments))
