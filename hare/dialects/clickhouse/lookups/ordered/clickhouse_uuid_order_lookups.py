from __future__ import annotations

from typing import Any

from hare.dialects.clickhouse.lookups.constants import CLICKHOUSE_TEXT_FUNCTION_NAME, CLICKHOUSE_TO_UUID_FUNCTION_NAME
from hare.dialects.clickhouse.lookups.ordered.clickhouse_order_key_lookups import ClickhouseOrderKeyLookups
from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper


class ClickhouseUuidOrderLookups(ClickhouseOrderKeyLookups):
    """UUIDs ordered and compared by their 16 bytes, as PostgreSQL orders them - ClickHouse's own
    order of a ``UUID`` takes its lower 64 bits first. Each side is compared as the UUID's text, whose
    lower-case hex digits order as its bytes do."""

    @staticmethod
    def get_order_key(term: Term) -> Term:
        return Function(CLICKHOUSE_TEXT_FUNCTION_NAME, term)

    @staticmethod
    def get_value_order_key(value: Any) -> Term:
        uuid_term = value if isinstance(value, Term) else ValueWrapper(value)
        return Function(CLICKHOUSE_TEXT_FUNCTION_NAME, Function(CLICKHOUSE_TO_UUID_FUNCTION_NAME, uuid_term))
