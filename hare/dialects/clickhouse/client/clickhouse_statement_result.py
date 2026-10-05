from __future__ import annotations

from typing import Any

from hare.dialects.base.results.statement_result import StatementResult


class ClickhouseStatementResult(StatementResult):
    """What a ClickHouse statement returned, with the totals row ``GROUP BY ... WITH TOTALS`` adds.

    Attributes:
        totals: The totals row, read as the rows are - None for a statement without it.
    """

    totals: Any = None
