from __future__ import annotations

from hare.dialects.clickhouse.lookups.constants import (
    CLICKHOUSE_JSON_HAS_KEY_SQL,
    CLICKHOUSE_JSON_KEY_EXISTENCE_FUNCTION_NAME,
    CLICKHOUSE_JSON_KEY_EXISTENCE_SQL,
)
from hare.sql.sql_context import SqlContext
from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term


class ClickhouseJsonKeyExistence(Function):
    """PostgreSQL's jsonb ``?``/``?&``/``?|`` over a JSON text column - a key of an object, a string
    element of an array or a string equal to the key; NULL for a NULL document."""

    def __init__(self, document: Term, keys: Term, *, every_key: bool, single_key: bool) -> None:
        """
        Args:
            document: The JSON text tested.
            keys: The key, or the array of keys.
            every_key: Whether every key has to exist, not any of them.
            single_key: Whether ``keys`` is one key, not an array.
        """
        super().__init__(CLICKHOUSE_JSON_KEY_EXISTENCE_FUNCTION_NAME, document, keys)
        self.every_key = every_key
        self.single_key = single_key

    def get_function_sql(self, sql_context: SqlContext) -> str:
        document_sql, keys_sql = (self.get_arg_sql(argument, sql_context) for argument in self.args)
        # The JSON text - of a JSON column, or of a String one holding it.
        document_sql = f"toString({document_sql})"
        return CLICKHOUSE_JSON_KEY_EXISTENCE_SQL.format(
            document=document_sql,
            array_function="arrayAll" if self.every_key else "arrayExists",
            has_key=CLICKHOUSE_JSON_HAS_KEY_SQL.format(document=f"ifNull({document_sql}, '')"),
            keys=f"[{keys_sql}]" if self.single_key else keys_sql,
        )
