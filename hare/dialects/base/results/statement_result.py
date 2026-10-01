from __future__ import annotations

from collections.abc import Sequence
from typing import Any


class StatementResult(tuple[int, Sequence[Any]]):
    """What one SQL statement returned - ``DatabaseClient.execute()``: the pair
    ``(row_count, rows)``, and the key of an inserted row where the driver reports one.

    Attributes:
        row_count: The rows the statement changed, for a write without ``RETURNING``; else the
            rows it returned.
        rows: The rows, as the driver gives them - each reads by column name, and by position
            where ``Features.supports_positional_rows``.
        inserted_id: The key the database gave the row an ``INSERT`` without ``RETURNING`` added,
            where the driver reports one (SQLite's last row id); None otherwise.
    """

    inserted_id: Any

    def __new__(cls, row_count: int, rows: Sequence[Any], inserted_id: Any = None) -> StatementResult:
        result = super().__new__(cls, (row_count, rows))
        result.inserted_id = inserted_id
        return result

    @property
    def row_count(self) -> int:
        return self[0]

    @property
    def rows(self) -> Sequence[Any]:
        return self[1]
