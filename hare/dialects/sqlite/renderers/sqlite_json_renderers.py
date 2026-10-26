from __future__ import annotations

import json

from hare.dialects.base.literals.constants import SQL_NULL_BYTE, SQL_NULL_BYTE_MESSAGE
from hare.dialects.base.renderers.json_renderers import JsonRenderers
from hare.dialects.sqlite.constants import SQLITE_JSON_BYTES_TEMPLATE, SQLITE_JSON_PATH_FUNCTION_NAME
from hare.dialects.sqlite.lookups.json.sqlite_json_type_criterion import SqliteJsonTypeCriterion
from hare.exceptions import ValidationError
from hare.sql import functions
from hare.sql.enums import JsonValueType
from hare.sql.sql_context import SqlContext
from hare.sql.terms import criteria


class SqliteJsonRenderers(JsonRenderers):
    """How SQLite writes JSON: objects and arrays built, a value read at a path and compared, and the
    tests of a key's presence and a value's type."""

    @staticmethod
    def render_json_object(function: functions.JsonObject, sql_context: SqlContext) -> str:
        return f"json_object({','.join(function.get_arg_sql(argument, sql_context) for argument in function.args)})"

    @staticmethod
    def render_json_array(function: functions.JsonArray, sql_context: SqlContext) -> str:
        return f"json_array({','.join(function.get_arg_sql(argument, sql_context) for argument in function.args)})"

    @staticmethod
    def render_json_value(function: functions.JsonValue, sql_context: SqlContext) -> str:
        """A value as SQLite writes it into JSON - a JSON value nested, bytes, which JSON holds none
        of, as their hex."""
        term_sql = function.get_arg_sql(function.args[0], sql_context)
        if function.value_type == JsonValueType.JSON:
            return f"json({term_sql})"
        if function.value_type == JsonValueType.BINARY:
            return SQLITE_JSON_BYTES_TEMPLATE.format(term_sql)
        return term_sql

    @classmethod
    def render_json_comparand(cls, function: functions.JsonComparand, sql_context: SqlContext) -> str:
        """A number as it is and any other value as JSON text - the form a path's value renders in."""
        term_sql = function.get_arg_sql(function.args[0], sql_context)
        if function.value_type == JsonValueType.PLAIN:
            return (
                f"CASE WHEN typeof({term_sql}) IN ('integer','real','null') THEN {term_sql} "
                f"ELSE json_quote({term_sql}) END"
            )
        value_sql = cls.render_json_value(function, sql_context)
        return f"CASE WHEN {term_sql} IS NULL THEN NULL ELSE json_quote({value_sql}) END"

    @classmethod
    def render_json_sort_key(cls, function: functions.JsonSortKey, sql_context: SqlContext) -> str:
        """The value SQLite's ``json_extract()`` reads - of a path or a whole JSON value, the value
        unwrapped (a string as itself, a boolean as 1 or 0, a JSON null as NULL, an object or an array
        as its text); of a typed expression compared with one, its own value."""
        argument = function.args[0]
        if isinstance(argument, criteria.JSONAttributeCriterion):
            return function.get_arg_sql(argument if argument.as_text else argument.get_text_term(), sql_context)
        if isinstance(argument, functions.JsonComparand):
            return cls.render_json_value(argument, sql_context)
        return f"json_extract({function.get_arg_sql(argument, sql_context)}, '$')"

    @classmethod
    def render_json_attribute(cls, criterion: criteria.JSONAttributeCriterion, sql_context: SqlContext) -> str:
        """``json_extract()`` - SQLite's ``->>``/``->`` operators need 3.38.0. The value as JSON is
        JSON text, except a number, which stays an SQL number so it compares and orders
        numerically."""
        column_sql = criterion.json_column.get_sql(sql_context)
        if any(isinstance(part, int) for part in criterion.path):
            return cls.get_json_path_function_sql(
                column_sql, criterion.path, "text" if criterion.as_text else "json", sql_context
            )
        path_sql = cls.get_json_native_path_sql(criterion.path)
        extract_sql = f"json_extract({column_sql}, {path_sql})"
        if criterion.as_text:
            return extract_sql
        return (
            f"CASE json_type({column_sql}, {path_sql}) WHEN 'true' THEN 'true' WHEN 'false' THEN 'false' "
            f"WHEN 'null' THEN 'null' WHEN 'text' THEN json_quote({extract_sql}) ELSE {extract_sql} END"
        )

    @classmethod
    def render_json_type(cls, criterion: SqliteJsonTypeCriterion, sql_context: SqlContext) -> str:
        column_sql = criterion.json_column.get_sql(sql_context)
        if any(isinstance(part, int) for part in criterion.path):
            return cls.get_json_path_function_sql(column_sql, criterion.path, "type", sql_context)
        return f"json_type({column_sql}, {cls.get_json_native_path_sql(criterion.path)})"

    @staticmethod
    def get_json_path_function_sql(column_sql: str, path: list[str | int], mode: str, sql_context: SqlContext) -> str:
        """A path with a digit segment, walked by hare's function - a digit segment is an object's
        key or an array's index, whichever the value at that point is, which no SQLite JSON path says.

        Args:
            column_sql: The JSON column.
            path: The path.
            mode: What the function returns: ``text`` (the unwrapped value), ``json`` or ``type``.
            sql_context: The context the SQL is written in.

        Raises:
            ValidationError: The path contains a null byte.
        """
        path_sql = SqliteJsonRenderers.get_json_path_array_sql(path)
        name = sql_context.note_connection_function(SQLITE_JSON_PATH_FUNCTION_NAME)
        return f"{name}({column_sql}, {path_sql}, '{mode}')"

    @staticmethod
    def get_json_path_array_sql(path: list[str | int]) -> str:
        """The literal of a path as the JSON array of its keys and indices hare's functions walk.

        Raises:
            ValidationError: The path contains a null byte.
        """
        path_json = json.dumps(path, ensure_ascii=False)
        if SQL_NULL_BYTE in path_json:
            raise ValidationError(SQL_NULL_BYTE_MESSAGE.format(text=path_json))
        return SqliteJsonRenderers.get_json_path_literal(path_json)

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
