from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.clickhouse.client.clickhouse_statement_result import ClickhouseStatementResult
from hare.dialects.clickhouse.query.totals_result import TotalsResult

if TYPE_CHECKING:
    from hare.query.queryset.extensions.query_set_extension_result import QuerySetExtensionResult


class ClickhouseTotals:
    """Shape ordinary grouped rows and the additional totals row of the same statement."""

    @staticmethod
    def transform_result(result: QuerySetExtensionResult) -> TotalsResult:
        """Decode totals through the same row layout and codecs as the ordinary result."""
        statement = result.statement_result
        totals = statement.totals if isinstance(statement, ClickhouseStatementResult) else None
        return TotalsResult(result.result, None if totals is None else result.read_row(totals))
