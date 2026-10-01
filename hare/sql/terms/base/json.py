from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.sql.context import SqlContext
from hare.sql.enums import JSONOperators

if TYPE_CHECKING:
    from hare.sql.queries.tables.table import Table
from hare.sql.terms import (  # noqa: E402
    array as array_terms,
    criteria as criteria_terms,
)
from hare.sql.terms.base.term import Term


class JSON(Term):
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
        pairs = [f"{self._recursive_get_sql(k)}:{self._recursive_get_sql(v)}" for k, v in value.items()]
        return "".join(["{", ",".join(pairs), "}"])

    def _get_list_sql(self, value: list[Any]) -> str:
        pairs = [self._recursive_get_sql(v) for v in value]
        return "".join(["[", ",".join(pairs), "]"])

    @staticmethod
    def _get_str_sql(value: str, quote_char: str = '"') -> str:
        return f"{quote_char}{value}{quote_char}"

    def get_sql(self, ctx: SqlContext) -> str:
        # SqlContext.quote_text() escapes embedded ctx.secondary_quote_char occurrences once, across
        # the whole assembled JSON text - equivalent to escaping each string leaf separately
        # first, since doubling a character commutes with string concatenation.
        sql = SqlContext.quote_text(self._recursive_get_sql(self.value), ctx.secondary_quote_char)
        return ctx.format_alias_sql(sql, self.alias)

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
