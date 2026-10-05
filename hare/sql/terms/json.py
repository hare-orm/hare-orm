from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.sql.enums import JSONOperators
from hare.sql.sql_context import SqlContext

if TYPE_CHECKING:
    from hare.sql.builder.tables.table import Table
from hare.sql.terms import (
    array as array_terms,
    criteria as criteria_terms,
)
from hare.sql.terms.term import Term


class JSON(Term):
    """A JSON value, and the JSON operators applied to a JSON term."""

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

    def get_json_value(self, key_or_index: str | int) -> criteria_terms.BasicCriterion:

        return criteria_terms.BasicCriterion(
            JSONOperators.GET_JSON_VALUE,
            self,
            self.wrap_constant(key_or_index),
        )

    def get_text_value(self, key_or_index: str | int) -> criteria_terms.BasicCriterion:

        return criteria_terms.BasicCriterion(
            JSONOperators.GET_TEXT_VALUE,
            self,
            self.wrap_constant(key_or_index),
        )

    def has_key(self, other: Any) -> criteria_terms.BasicCriterion:

        return criteria_terms.BasicCriterion(
            JSONOperators.HAS_KEY,
            self,
            self.wrap_json(other),
        )

    def contains(self, other: Any) -> criteria_terms.BasicCriterion:

        return criteria_terms.BasicCriterion(
            JSONOperators.CONTAINS,
            self,
            self.wrap_json(other),
        )

    def contained_by(self, other: Any) -> criteria_terms.BasicCriterion:

        return criteria_terms.BasicCriterion(
            JSONOperators.CONTAINED_BY,
            self,
            self.wrap_json(other),
        )

    def has_keys(self, other: Iterable[Any]) -> criteria_terms.BasicCriterion:

        return criteria_terms.BasicCriterion(JSONOperators.HAS_KEYS, self, array_terms.Array(*other))

    def has_any_keys(self, other: Iterable[Any]) -> criteria_terms.BasicCriterion:

        return criteria_terms.BasicCriterion(JSONOperators.HAS_ANY_KEYS, self, array_terms.Array(*other))
