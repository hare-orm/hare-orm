from __future__ import annotations

from hare.dialects.clickhouse.renderers.clickhouse_value_texts import ClickhouseValueTexts
from hare.dialects.clickhouse.renderers.constants import (
    CLICKHOUSE_JSON_BOOLEAN_TEXT_TEMPLATE,
    CLICKHOUSE_JSON_BYTES_TEXT_TEMPLATE,
    CLICKHOUSE_JSON_FLOAT_TEXT_TEMPLATE,
    CLICKHOUSE_JSON_PLAIN_TEXT_TEMPLATE,
    CLICKHOUSE_JSON_STRING_TEMPLATE,
    CLICKHOUSE_JSON_VALUE_TEXT_TEMPLATE,
    CLICKHOUSE_TIME_TEXT_TEMPLATE,
)
from hare.sql.enums import JsonValueType
from hare.sql.functions.json.json_value import JsonValue
from hare.sql.sql_context import SqlContext


class ClickhouseJsonTexts:
    """A value as the JSON text PostgreSQL's ``jsonb`` holds of it - ClickHouse builds a JSON object
    or array as text, a map or a tuple of it holding values of fixed types only: a boolean as
    ``true``/``false``, a Decimal and a float as a number, a moment as ISO text in UTC, a time with its
    offset, bytes as a backslash, ``x`` and their hex, a JSON value nested."""

    @staticmethod
    def get_json_text_sql(json_value: JsonValue, sql_context: SqlContext) -> str:
        """The SQL of a value's JSON text.

        Args:
            json_value: The value with its JSON type.
            sql_context: The context the value renders in.

        Returns:
            The SQL.
        """
        term_sql = json_value.get_arg_sql(json_value.args[0], sql_context)
        value_type = json_value.value_type
        if value_type == JsonValueType.BOOLEAN:
            return CLICKHOUSE_JSON_BOOLEAN_TEXT_TEMPLATE.format(term_sql)
        if value_type == JsonValueType.DECIMAL:
            # Every place of the Decimal's type - jsonb keeps a numeric's scale.
            return CLICKHOUSE_JSON_VALUE_TEXT_TEMPLATE.format(ClickhouseValueTexts.get_decimal_text_sql(term_sql))
        if value_type == JsonValueType.JSON:
            return CLICKHOUSE_JSON_VALUE_TEXT_TEMPLATE.format(term_sql)
        if value_type == JsonValueType.FLOAT:
            return CLICKHOUSE_JSON_FLOAT_TEXT_TEMPLATE.format(term_sql)
        if value_type == JsonValueType.DATETIME:
            moment_sql = ClickhouseValueTexts.get_moment_text_sql(term_sql, is_aware=False)
            offset = "+00:00" if json_value.is_aware else ""
            return CLICKHOUSE_JSON_STRING_TEMPLATE.format(term_sql, f"replaceOne({moment_sql}, ' ', 'T'), '{offset}'")
        if value_type == JsonValueType.TIME:
            return CLICKHOUSE_JSON_STRING_TEMPLATE.format(term_sql, CLICKHOUSE_TIME_TEXT_TEMPLATE.format(term_sql))
        if value_type == JsonValueType.BINARY:
            bytes_sql = CLICKHOUSE_JSON_BYTES_TEXT_TEMPLATE.format(term_sql)
            return CLICKHOUSE_JSON_STRING_TEMPLATE.format(term_sql, bytes_sql)
        return CLICKHOUSE_JSON_PLAIN_TEXT_TEMPLATE.format(term_sql)
