from __future__ import annotations

from hare.sql.enums import AnalyticFunctionName

#: The aggregates a row read more than once doesn't change - a correlated subquery selecting one runs
#: where the database reads each row once per outer row of its key.
ROW_COUNT_FREE_AGGREGATE_NAMES = frozenset(
    {AnalyticFunctionName.MAX, AnalyticFunctionName.MIN, AnalyticFunctionName.AVG}
)
