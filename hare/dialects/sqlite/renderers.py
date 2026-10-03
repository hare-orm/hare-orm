from __future__ import annotations

import json
from typing import Any, ClassVar

from hare.dialects.base.renderers.dialect_renderers import DialectRenderers
from hare.dialects.base.renderers.term_renderers import TermRenderers
from hare.dialects.sqlite.constants import (
    SQLITE_CAST_FUNCTION_NAME,
    SQLITE_DATE_DIFFERENCE_FUNCTION_NAME,
    SQLITE_DATE_SHIFT_FUNCTION_NAME,
    SQLITE_DATE_TIMESTAMP_FUNCTION_NAME,
    SQLITE_DATETIME_DIFFERENCE_FUNCTION_NAME,
    SQLITE_DATETIME_SHIFT_FUNCTION_NAME,
    SQLITE_DECIMAL_MOD_MAX_SCALE,
    SQLITE_EXTRACT_FUNCTION_NAME,
    SQLITE_GREATEST_FUNCTION_NAME,
    SQLITE_JSON_BYTES_FUNCTION_NAME,
    SQLITE_JSON_FLOAT_FUNCTION_NAME,
    SQLITE_JSON_PATH_FUNCTION_NAME,
    SQLITE_JSON_SORT_KEY_FUNCTION_NAME,
    SQLITE_JSON_TIME_FUNCTION_NAME,
    SQLITE_JSON_TIMESTAMP_FUNCTION_NAME,
    SQLITE_LEAST_FUNCTION_NAME,
    SQLITE_LOWER_FUNCTION_NAME,
    SQLITE_MATH_FUNCTION_PREFIX,
    SQLITE_NATIVE_TEXT_FUNCTIONS,
    SQLITE_NUMBER_TEXT_FUNCTION_NAME,
    SQLITE_STATISTICS_FUNCTION_NAMES,
    SQLITE_TEXT_FUNCTION_PREFIX,
    SQLITE_TRUNC_FUNCTION_NAME,
    SQLITE_UPPER_FUNCTION_NAME,
)
from hare.dialects.sqlite.defaults import SqliteDefaults
from hare.exceptions import ValidationError
from hare.fields.db_defaults.now import Now
from hare.sql import analytics, functions
from hare.sql.constants import SQL_NULL_BYTE, SQL_NULL_BYTE_MESSAGE
from hare.sql.context import SqlContext
from hare.sql.enums import JsonValueType
from hare.sql.exceptions import FunctionException
from hare.sql.terms import criteria, functions as term_functions
from hare.sql.terms.base.value_wrapper import ValueWrapper


class SqliteRenderers(DialectRenderers):
    """How SQLite renders the terms whose SQL differs between dialects - mostly as calls of hare's
    own SQLite functions, which reproduce PostgreSQL's semantics."""

    #: The function writing each type of JSON value that needs one.
    JSON_VALUE_FUNCTION_NAMES: ClassVar[dict[JsonValueType, str]] = {
        JsonValueType.FLOAT: SQLITE_JSON_FLOAT_FUNCTION_NAME,
        JsonValueType.DATETIME: SQLITE_JSON_TIMESTAMP_FUNCTION_NAME,
        JsonValueType.TIME: SQLITE_JSON_TIME_FUNCTION_NAME,
        JsonValueType.BINARY: SQLITE_JSON_BYTES_FUNCTION_NAME,
    }

    @classmethod
    def add_own_renderers(cls, renderers: TermRenderers) -> None:
        renderers.register(functions.Statistic, cls.render_statistic)
        renderers.register(functions.Concat, cls.render_concat)
        renderers.register(functions.Now, cls.render_now)
        renderers.register(functions.GreatestLeast, cls.render_greatest_least)
        renderers.register(term_functions.Mod, cls.render_mod)
        renderers.register(term_functions.DecimalMod, cls.render_decimal_mod)
        renderers.register_name(functions.Upper, cls.get_upper_name)
        renderers.register_name(functions.Lower, cls.get_lower_name)
        renderers.register_name(analytics.Statistic, cls.get_statistic_name)
        SqliteDefaults.register(renderers)

    @staticmethod
    def render_statistic(function: functions.Statistic, ctx: SqlContext) -> str:
        return function.get_statistic_sql(SQLITE_STATISTICS_FUNCTION_NAMES[function.name], ctx)

    @staticmethod
    def render_boolean_as_text(function: functions.BooleanAsText, ctx: SqlContext) -> str:
        term_sql = function.get_arg_sql(function.args[0], ctx)
        return f"CASE {term_sql} WHEN 1 THEN 'true' WHEN 0 THEN 'false' END"

    @staticmethod
    def render_float_as_text(function: functions.FloatAsText, ctx: SqlContext) -> str:
        return f"{SQLITE_NUMBER_TEXT_FUNCTION_NAME}({function.get_arg_sql(function.args[0], ctx)},NULL)"

    @staticmethod
    def render_decimal_as_text(function: functions.DecimalAsText, ctx: SqlContext) -> str:
        return f"{SQLITE_NUMBER_TEXT_FUNCTION_NAME}({function.get_arg_sql(function.args[0], ctx)},{function.scale})"

    @staticmethod
    def render_datetime_as_text(function: functions.DatetimeAsText, ctx: SqlContext) -> str:
        # A timestamp column already holds this text.
        return function.get_arg_sql(function.args[0], ctx)

    @staticmethod
    def render_extract(function: functions.Extract, ctx: SqlContext) -> str:
        # SQLite has no EXTRACT() and no IANA zones - a function extracts in the zone itself.
        date_part_sql = ValueWrapper(function.date_part.value).get_sql(ctx)
        field_sql = function.field.get_sql(ctx)
        zone_sql = ValueWrapper(function.zone_name).get_sql(ctx)
        return f"{SQLITE_EXTRACT_FUNCTION_NAME}({date_part_sql}, {field_sql}, {zone_sql})"

    @staticmethod
    def render_math_function(function: functions.MathFunction, ctx: SqlContext) -> str:
        args_sql = [function.get_arg_sql(arg, ctx) for arg in function.args]
        return f"{SQLITE_MATH_FUNCTION_PREFIX}{function.name.lower()}({','.join(args_sql)})"

    @staticmethod
    def render_cast_to(function: functions.CastTo, ctx: SqlContext) -> str:
        term_sql = function.get_arg_sql(function.args[0], ctx)
        arguments_sql = [
            ValueWrapper(function.target.value, allow_parametrize=False).get_sql(ctx),
            ValueWrapper(function.source.value, allow_parametrize=False).get_sql(ctx),
            *("NULL" if parameter is None else str(int(parameter)) for parameter in function.parameters),
            str(int(function.is_aware)),
        ]
        return f"{SQLITE_CAST_FUNCTION_NAME}({term_sql}, {', '.join(arguments_sql)})"

    @staticmethod
    def render_greatest_least(function: functions.GreatestLeast, ctx: SqlContext) -> str:
        args_sql = [function.get_arg_sql(arg, ctx) for arg in function.args]
        name = SQLITE_GREATEST_FUNCTION_NAME if function.name == "GREATEST" else SQLITE_LEAST_FUNCTION_NAME
        comparison_type_sql = ValueWrapper(
            "number" if function.compares_numbers else "value", allow_parametrize=False
        ).get_sql(ctx)
        return f"{name}({comparison_type_sql}, {', '.join(args_sql)})"

    @staticmethod
    def render_concat(function: functions.Concat, ctx: SqlContext) -> str:
        # CONCAT() needs SQLite 3.44; `||` with each NULL read as empty text gives the same result
        # on every supported version.
        args_sql = [f"COALESCE({function.get_arg_sql(arg, ctx)}, '')" for arg in function.args]
        return f"({' || '.join(args_sql)})"

    @staticmethod
    def render_now(function: functions.Now, ctx: SqlContext) -> str:
        # SQLite has no NOW(): the current moment as the text a DatetimeField value is written as.
        return SqliteDefaults.render_now(Now(), ctx)

    @staticmethod
    def render_text_function(function: functions.TextFunction, ctx: SqlContext) -> str:
        args_sql = [function.get_arg_sql(arg, ctx) for arg in function.args]
        name = SQLITE_NATIVE_TEXT_FUNCTIONS.get(function.name, f"{SQLITE_TEXT_FUNCTION_PREFIX}{function.name.lower()}")
        return f"{name}({','.join(args_sql)})"

    @staticmethod
    def render_date_trunc(function: functions.DateTrunc, ctx: SqlContext) -> str:
        field_sql = function.field.get_sql(ctx)
        trunc_type_sql = ValueWrapper(function.trunc_type.value, allow_parametrize=False).get_sql(ctx)
        zone_sql = ValueWrapper(function.zone_name).get_sql(ctx)
        return f"{SQLITE_TRUNC_FUNCTION_NAME}({trunc_type_sql}, {field_sql}, {zone_sql})"

    @staticmethod
    def render_datetime_cast(function: functions.DatetimeCast, ctx: SqlContext) -> str:
        field_sql = function.field.get_sql(ctx)
        target_sql = ValueWrapper(function.target.value).get_sql(ctx)
        zone_sql = ValueWrapper(function.zone_name).get_sql(ctx)
        return f"{SQLITE_EXTRACT_FUNCTION_NAME}({target_sql}, {field_sql}, {zone_sql})"

    @staticmethod
    def render_temporal_shift(function: functions.TemporalShift, ctx: SqlContext) -> str:
        if ctx.native_functions_only:
            raise FunctionException("Date/datetime arithmetic has no dialect-neutral SQL form on SQLite")
        base_term, microseconds_term = function.args
        function_name = SQLITE_DATE_SHIFT_FUNCTION_NAME if function.is_date else SQLITE_DATETIME_SHIFT_FUNCTION_NAME
        sign = -1 if function.is_subtraction else 1
        base_sql = function.get_arg_sql(base_term, ctx)
        return f"{function_name}({base_sql},{function.get_arg_sql(microseconds_term, ctx)},{sign})"

    @staticmethod
    def render_temporal_difference(function: functions.TemporalDifference, ctx: SqlContext) -> str:
        if ctx.native_functions_only:
            raise FunctionException("Date/datetime arithmetic has no dialect-neutral SQL form on SQLite")
        left_term, right_term = function.args
        function_name = (
            SQLITE_DATE_DIFFERENCE_FUNCTION_NAME if function.is_date else SQLITE_DATETIME_DIFFERENCE_FUNCTION_NAME
        )
        return f"{function_name}({function.get_arg_sql(left_term, ctx)},{function.get_arg_sql(right_term, ctx)})"

    @staticmethod
    def render_json_object(function: functions.JsonObject, ctx: SqlContext) -> str:
        return f"json_object({','.join(function.get_arg_sql(argument, ctx) for argument in function.args)})"

    @classmethod
    def render_json_value(cls, function: functions.JsonValue, ctx: SqlContext) -> str:
        """A value written as PostgreSQL's jsonb holds it - a boolean as ``true``/``false``, a
        Decimal as a number, a timestamp as ISO text in UTC, a JSON value nested."""
        term_sql = function.get_arg_sql(function.args[0], ctx)
        function_names = cls.JSON_VALUE_FUNCTION_NAMES
        if function.value_type == JsonValueType.DATETIME and not function.is_aware:
            return f"{function_names[function.value_type]}({term_sql},0)"
        if function.value_type == JsonValueType.BOOLEAN:
            return f"json(CASE {term_sql} WHEN 1 THEN 'true' WHEN 0 THEN 'false' END)"
        if function.value_type in (JsonValueType.DECIMAL, JsonValueType.JSON):
            return f"json({term_sql})"
        if function.value_type == JsonValueType.FLOAT:
            return f"json({function_names[function.value_type]}({term_sql}))"
        if function.value_type == JsonValueType.DATETIME:
            return f"{function_names[function.value_type]}({term_sql},1)"
        if function.value_type in function_names:
            return f"{function_names[function.value_type]}({term_sql})"
        return term_sql

    @classmethod
    def render_json_comparand(cls, function: functions.JsonComparand, ctx: SqlContext) -> str:
        """A number as it is and any other value as JSON text."""
        term_sql = function.get_arg_sql(function.args[0], ctx)
        if function.value_type == JsonValueType.DECIMAL:
            return f"CAST({term_sql} AS NUMERIC)"
        if function.value_type == JsonValueType.FLOAT:
            return term_sql
        if function.value_type == JsonValueType.PLAIN:
            return (
                f"CASE WHEN typeof({term_sql}) IN ('integer','real','null') THEN {term_sql} "
                f"ELSE json_quote({term_sql}) END"
            )
        return f"CASE WHEN {term_sql} IS NULL THEN NULL ELSE json_quote({cls.render_json_value(function, ctx)}) END"

    @staticmethod
    def render_date_as_timestamp(function: functions.DateAsTimestamp, ctx: SqlContext) -> str:
        term_sql = function.get_arg_sql(function.args[0], ctx)
        zone_sql = (
            "NULL"
            if function.zone_name is None
            else ValueWrapper(function.zone_name, allow_parametrize=False).get_sql(ctx)
        )
        return f"{SQLITE_DATE_TIMESTAMP_FUNCTION_NAME}({term_sql},{zone_sql})"

    @staticmethod
    def render_json_sort_key(function: functions.JsonSortKey, ctx: SqlContext) -> str:
        """A byte key ordering JSON values as PostgreSQL orders jsonb."""
        term_sql = function.get_arg_sql(function.args[0], ctx)
        return f"{SQLITE_JSON_SORT_KEY_FUNCTION_NAME}({term_sql})"

    @staticmethod
    def render_interval(interval: term_functions.Interval, ctx: SqlContext) -> str:
        raise FunctionException(
            "Interval(...) has no SQLite rendering - SQLite has no INTERVAL literal; build "
            "the equivalent date()/datetime() modifier expression directly for this dialect."
        )

    @staticmethod
    def render_mod(function: term_functions.Mod, ctx: SqlContext) -> str:
        """The integer ``%`` operator for integers - SQLite's ``MOD()`` computes in floating point."""
        if not function.integer:
            return term_functions.Function.get_function_sql(function, ctx)
        left_sql, right_sql = (function.get_arg_sql(arg, ctx) for arg in function.args)
        return f"(({left_sql})%({right_sql}))"

    @staticmethod
    def render_float_mod(function: term_functions.FloatMod, ctx: SqlContext) -> str:
        """SQLite's floating-point ``MOD()``; ``+ 0.0`` turns a negative zero into PostgreSQL's positive one."""
        return f"({term_functions.Function.get_function_sql(function, ctx)}+0.0)"

    @staticmethod
    def render_decimal_mod(function: term_functions.DecimalMod, ctx: SqlContext) -> str:
        """Both operands scaled to whole numbers and the integer remainder scaled back - SQLite's
        ``MOD()`` runs on doubles. Beyond the scale a 64-bit integer holds, or with an unknown
        scale, it is ``MOD()`` itself."""
        if function.scale is None or function.scale > SQLITE_DECIMAL_MOD_MAX_SCALE:
            return term_functions.Function.get_function_sql(function, ctx)
        factor = 10**function.scale
        left_sql, right_sql = (
            f"CAST(ROUND(({function.get_arg_sql(arg, ctx)})*{factor}) AS INTEGER)" for arg in function.args
        )
        return f"(({left_sql}%{right_sql})/{factor}.0)"

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
        path_sql = SqliteRenderers.get_json_path_literal(path_json)
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
        return SqliteRenderers.get_json_path_literal("$" + "".join(segments))

    @classmethod
    def render_json_attribute(cls, criterion: criteria.JSONAttributeCriterion, ctx: SqlContext) -> str:
        """``json_extract()`` - SQLite's ``->>``/``->`` operators need 3.38.0. The value as JSON is
        JSON text, except a number, which stays an SQL number so it compares and orders
        numerically."""
        column_sql = criterion.json_column.get_sql(ctx)
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
    def render_json_type(cls, criterion: criteria.JSONTypeCriterion, ctx: SqlContext) -> str:
        column_sql = criterion.json_column.get_sql(ctx)
        if any(isinstance(part, int) for part in criterion.path):
            return cls.get_json_path_function_sql(column_sql, criterion.path, "type")
        return f"json_type({column_sql}, {cls.get_json_native_path_sql(criterion.path)})"

    @staticmethod
    def get_upper_name(function: Any, ctx: SqlContext) -> str:
        # hare's function case-folds as Python's str.upper() does; SQLite's own UPPER() folds
        # ASCII only. Not in SQL stored in DDL or a migration file, where only native functions go.
        return "" if ctx.native_functions_only else SQLITE_UPPER_FUNCTION_NAME

    @staticmethod
    def get_lower_name(function: Any, ctx: SqlContext) -> str:
        return "" if ctx.native_functions_only else SQLITE_LOWER_FUNCTION_NAME

    @staticmethod
    def get_statistic_name(function: analytics.Statistic, ctx: SqlContext) -> str:
        return SQLITE_STATISTICS_FUNCTION_NAMES[str(function.name)]
