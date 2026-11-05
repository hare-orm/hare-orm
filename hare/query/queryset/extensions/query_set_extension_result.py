from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.exceptions import QueryError

if TYPE_CHECKING:
    from hare.dialects.base.results.statement_result import StatementResult


@dataclass(frozen=True, slots=True)
class QuerySetExtensionResult:
    """The already executed result available to one result-shaping extension.

    Result handlers cannot replace statement execution. Values queries provide the
    raw statement metadata and a decoder for additional driver rows, using the same
    field conversions and row shape as the ordinary result. Model queries provide
    their materialized result without exposing driver rows.
    """

    result: Any
    statement_result: StatementResult | None = None
    row_decoder: Callable[[Any], Any] | None = None

    def read_row(self, row: Any) -> Any:
        """Decode one additional driver row, such as a totals row."""
        if self.row_decoder is None:
            raise QueryError("This query does not expose an additional-row decoder")
        return self.row_decoder(row)
