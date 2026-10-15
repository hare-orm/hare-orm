from __future__ import annotations

from decimal import Decimal

from hare.dialects.base.parameters.sql_parameters import SqlParameters
from hare.dialects.sqlite.parameters.constants import SQLITE_IN_JSON_ARRAY_THRESHOLD


class SqliteParameters(SqlParameters):
    """SQLite's parameters - a long ``__in`` list bound as one JSON array, a number standing for a
    JSON number bound as SQLite can hold it."""

    single_parameter_in_list_min_length = SQLITE_IN_JSON_ARRAY_THRESHOLD

    def get_bindable_number(self, value: int | float | Decimal) -> int | float | Decimal:
        # Local import: the constants module instantiates the dialect, which imports this one.
        from hare.dialects.sqlite.constants import SQLITE_INTEGER_MAX, SQLITE_INTEGER_MIN

        # A JSON integer outside 64 bits is held as a REAL, and can't be bound as an int.
        if isinstance(value, int) and not SQLITE_INTEGER_MIN <= value <= SQLITE_INTEGER_MAX:
            return float(value)
        return value
