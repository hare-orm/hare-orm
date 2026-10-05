from __future__ import annotations

from typing import Any

from hare.dialects.clickhouse.lookups.clickhouse_time_sort_key import ClickhouseTimeSortKey
from hare.dialects.clickhouse.lookups.ordered.clickhouse_order_key_lookups import ClickhouseOrderKeyLookups
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper


class ClickhouseTimeOrderLookups(ClickhouseOrderKeyLookups):
    """Times of day ordered and compared as PostgreSQL orders ``TIMETZ`` - by the UTC time, then by the
    offset; ClickHouse holds them as text."""

    @staticmethod
    def get_order_key(term: Term) -> Term:
        return ClickhouseTimeSortKey(term)

    @staticmethod
    def get_value_order_key(value: Any) -> Term:
        return ClickhouseTimeSortKey(value if isinstance(value, Term) else ValueWrapper(value))
