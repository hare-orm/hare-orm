from __future__ import annotations

from typing import Any


class TotalsResult(list[Any]):
    """The rows of a ``with_totals()`` query, with the row of its aggregates over every group.

    Attributes:
        totals: The totals row, read as the query's rows are - its grouped fields their types' default
            values; None where the server sent none.
    """

    def __init__(self, rows: list[Any], totals: Any) -> None:
        """
        Args:
            rows: The query's rows.
            totals: The totals row.
        """
        super().__init__(rows)
        self.totals = totals
