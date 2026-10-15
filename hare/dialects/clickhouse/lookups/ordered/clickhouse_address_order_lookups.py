from __future__ import annotations

from typing import Any

from hare.dialects.clickhouse.lookups.clickhouse_address_sort_key import ClickhouseAddressSortKey
from hare.dialects.clickhouse.lookups.constants import CLICKHOUSE_TEXT_FUNCTION_NAME, CLICKHOUSE_TO_IPV6_FUNCTION_NAME
from hare.dialects.clickhouse.lookups.ordered.clickhouse_order_key_lookups import ClickhouseOrderKeyLookups
from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper


class ClickhouseAddressOrderLookups(ClickhouseOrderKeyLookups):
    """IP addresses ordered and compared as PostgreSQL orders ``inet`` - IPv4 before IPv6; ClickHouse
    holds an IPv4 address as an IPv6 one, which its own order puts after ``::1``."""

    @staticmethod
    def get_order_key(term: Term) -> Term:
        return ClickhouseAddressSortKey(term)

    @staticmethod
    def get_value_order_key(value: Any) -> Term:
        value_term = value if isinstance(value, Term) else ValueWrapper(value)
        return ClickhouseAddressSortKey(
            Function(CLICKHOUSE_TO_IPV6_FUNCTION_NAME, Function(CLICKHOUSE_TEXT_FUNCTION_NAME, value_term))
        )
