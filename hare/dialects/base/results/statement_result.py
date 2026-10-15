from __future__ import annotations

from collections.abc import Sequence
from operator import itemgetter
from typing import Any


class StatementResult(tuple[int, Sequence[Any]]):
    """What one SQL statement returned - ``DatabaseClient.execute()``: the pair
    ``(row_count, rows)``, and the key of an inserted row where the driver reports one.

    Attributes:
        row_count: The rows the statement changed, for a write without ``RETURNING``; else the
            rows it returned.
        rows: The rows, as the driver gives them - each reads by column name, and by position
            where ``Features.supports_positional_rows``; rows asked for by position
            (``execute(rows_by_position=True)``) may read by position alone.
        inserted_id: The key the database gave the row an ``INSERT`` without ``RETURNING`` added,
            where the driver reports one (SQLite's last row id); None otherwise.
        description: The DB-API cursor description of the rows - each column's name first - where
            the driver gives one; None when the names are read off the first row.
    """

    # Read off the pair itself: a result is made for every query, and an instance dictionary is
    # made only for the attributes set below.
    row_count: int = property(itemgetter(0))  # type: ignore[assignment]
    rows: Sequence[Any] = property(itemgetter(1))  # type: ignore[assignment]
    inserted_id: Any = None
    description: Sequence[Sequence[Any]] | None = None

    def __new__(
        cls,
        row_count: int,
        rows: Sequence[Any],
        inserted_id: Any = None,
        description: Sequence[Sequence[Any]] | None = None,
    ) -> StatementResult:
        result = tuple.__new__(cls, (row_count, rows))
        if inserted_id is not None:
            result.inserted_id = inserted_id
        if description is not None:
            result.description = description
        return result

    @property
    def column_names(self) -> tuple[str, ...]:
        """The names of the columns of the rows, in order - read from the description, or from the
        first row's names when the rows read by name; empty for no rows read by name."""
        description = self.description
        if description is not None:
            return tuple([column[0] for column in description])
        rows = self.rows
        return tuple(rows[0].keys()) if rows else ()
