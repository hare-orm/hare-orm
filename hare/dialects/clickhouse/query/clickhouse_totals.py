from __future__ import annotations

from typing import TYPE_CHECKING, cast

from hare.dialects.clickhouse.client.clickhouse_statement_result import ClickhouseStatementResult
from hare.dialects.clickhouse.query.totals_result import TotalsResult

if TYPE_CHECKING:
    from hare.query.queryset.extensions.query_set_extension_query import QuerySetExtensionQuery
    from hare.query.statements.select.values_query import ValuesQuery


class ClickhouseTotals:
    """The rows of a ``with_totals()`` query with its totals row - both read from the one statement
    ``GROUP BY ... WITH TOTALS``: the query's aggregates over all the rows it reads, of the groups its
    ``HAVING`` keeps, before ``ORDER BY``, ``LIMIT BY`` and ``LIMIT``."""

    @staticmethod
    async def run(extension_query: QuerySetExtensionQuery) -> TotalsResult:
        """Runs the query, built.

        Args:
            extension_query: The query.

        Returns:
            The rows with the totals row.
        """
        query = cast("ValuesQuery", extension_query.query)
        values_reading = query._values_reading or query.get_values_reading()
        column_converters = values_reading.column_converters
        result = await query._connection.execute(*query._get_parameterized_sql(), returns_rows=True)
        rows = [query._convert_row(dict(row), column_converters) for row in result.rows]
        totals = result.totals if isinstance(result, ClickhouseStatementResult) else None
        return TotalsResult(rows, None if totals is None else query._convert_row(dict(totals), column_converters))
