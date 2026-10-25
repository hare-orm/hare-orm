from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql.sql_context import SqlContext
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from hare.sql.builder.tables.table import Table


class JSON(Term):
    """A JSON value, written as the dialect's string literal of its text."""

    table: Table | None = None

    def __init__(self, value: Any = None, alias: str | None = None) -> None:
        super().__init__(alias)
        self.value = value

    def _recursive_get_sql(self, value: Any) -> str:
        if isinstance(value, dict):
            return self._get_dict_sql(value)
        if isinstance(value, list):
            return self._get_list_sql(value)
        if isinstance(value, str):
            return self._get_str_sql(value)
        return str(value)

    def _get_dict_sql(self, value: dict[Any, Any]) -> str:
        pairs = [f"{self._recursive_get_sql(key)}:{self._recursive_get_sql(item)}" for key, item in value.items()]
        return "".join(["{", ",".join(pairs), "}"])

    def _get_list_sql(self, value: list[Any]) -> str:
        pairs = [self._recursive_get_sql(item) for item in value]
        return "".join(["[", ",".join(pairs), "]"])

    @staticmethod
    def _get_str_sql(value: str, quote_char: str = '"') -> str:
        return f"{quote_char}{value}{quote_char}"

    def get_sql(self, sql_context: SqlContext) -> str:
        # The whole assembled JSON text is the dialect's string literal - escaped once, as escaping each
        # string leaf separately first would be.
        sql = sql_context.dialect.literals.get_string_literal_sql(self._recursive_get_sql(self.value))
        return sql_context.format_alias_sql(sql, self.alias)
