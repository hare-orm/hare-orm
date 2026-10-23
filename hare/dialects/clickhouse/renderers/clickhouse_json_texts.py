from __future__ import annotations

from hare.dialects.clickhouse.renderers.constants import (
    CLICKHOUSE_JSON_BYTES_TEXT_TEMPLATE,
    CLICKHOUSE_JSON_PLAIN_TEXT_TEMPLATE,
    CLICKHOUSE_JSON_VALUE_TEXT_TEMPLATE,
)
from hare.sql.enums import JsonValueType
from hare.sql.functions.json.json_value import JsonValue
from hare.sql.sql_context import SqlContext


class ClickhouseJsonTexts:
    """A value as the JSON text ClickHouse writes of it (``toJSONString()``) - ClickHouse builds a JSON
    object or array as text: a JSON value nested, bytes, which ``toJSONString()`` would write as text
    of their invalid characters, as their hex."""

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
        if json_value.value_type == JsonValueType.JSON:
            return CLICKHOUSE_JSON_VALUE_TEXT_TEMPLATE.format(term_sql)
        if json_value.value_type == JsonValueType.BINARY:
            return CLICKHOUSE_JSON_BYTES_TEXT_TEMPLATE.format(term_sql)
        return CLICKHOUSE_JSON_PLAIN_TEXT_TEMPLATE.format(term_sql)
