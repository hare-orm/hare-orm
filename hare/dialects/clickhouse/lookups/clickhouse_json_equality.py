from __future__ import annotations

from typing import Any

from hare.dialects.clickhouse.lookups.constants import (
    CLICKHOUSE_JSON_MEMBER_PREFIX,
    CLICKHOUSE_JSON_MEMBER_SUFFIX,
    CLICKHOUSE_JSON_TYPE_NAME,
)
from hare.dialects.clickhouse.lookups.in_list.clickhouse_large_in_list import ClickhouseLargeInList
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper


class ClickhouseJsonEquality:
    """``JSONField`` equality and ``__in`` on a server with the ``JSON`` type, as PostgreSQL's ``jsonb``
    compares: each side as the server's canonical text of the value - keys in their order, ``2.0`` as
    ``2`` - so the key order and the spelling of a number written don't matter. A ``JSON`` column takes
    no ``IN``, and a text of a ``String`` column is compared as it was written otherwise.

    Args:
        stores_json_natively: Whether the column is a ``JSON`` one - else a ``String`` of JSON text.
    """

    def __init__(self, stores_json_natively: bool) -> None:
        self.stores_json_natively = stores_json_natively

    def get_canonical_term(self, term: Any, *, is_column: bool) -> Term:
        """A JSON value's canonical text - of any JSON value, written as the member of an object.

        Args:
            term: The column, or a value's JSON text.
            is_column: Whether ``term`` is the column.

        Returns:
            The term of the text, NULL for NULL.
        """
        text = Function("toJSONString", term) if is_column and self.stores_json_natively else term
        member = Function(
            "concat",
            ValueWrapper(CLICKHOUSE_JSON_MEMBER_PREFIX, allow_parametrize=False),
            text,
            ValueWrapper(CLICKHOUSE_JSON_MEMBER_SUFFIX, allow_parametrize=False),
        )
        return Function(
            "toJSONString", Function("CAST", member, ValueWrapper(CLICKHOUSE_JSON_TYPE_NAME, allow_parametrize=False))
        )

    def equal(self, term: Term, value: Any) -> Criterion:
        """``field=value`` on a ``JSONField``."""
        if value is None:
            return term.isnull()
        return self.get_canonical_term(term, is_column=True) == self.get_canonical_term(value, is_column=False)

    def not_equal(self, term: Term, value: Any) -> Criterion:
        """``field__not=value`` on a ``JSONField`` - a NULL row is not equal."""
        if value is None:
            return term.notnull()
        return (
            self.get_canonical_term(term, is_column=True) != self.get_canonical_term(value, is_column=False)
        ) | term.isnull()

    def is_in(self, term: Term, value: Any) -> Criterion:
        """``field__in=[...]`` on a ``JSONField`` - a None in the list matches a NULL row."""
        if not isinstance(value, list | tuple | set):
            return ClickhouseLargeInList.is_in(term, value)
        texts = [item for item in value if item is not None]
        criterion = Criterion.any(
            self.get_canonical_term(term, is_column=True) == self.get_canonical_term(text, is_column=False)
            for text in texts
        )
        return criterion | term.isnull() if len(texts) < len(value) else criterion

    def not_in(self, term: Term, value: Any) -> Criterion:
        """``field__not_in=[...]`` on a ``JSONField`` - a NULL row is not in a list holding no None."""
        if not isinstance(value, list | tuple | set):
            return ClickhouseLargeInList.not_in(term, value)
        texts = [item for item in value if item is not None]
        criterion = Criterion.all(
            self.get_canonical_term(term, is_column=True) != self.get_canonical_term(text, is_column=False)
            for text in texts
        )
        if len(texts) < len(value):
            return criterion & term.notnull()
        return criterion | term.isnull()
