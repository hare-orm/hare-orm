from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hare.fields.narrowing.narrowing_limit import NarrowingLimit

if TYPE_CHECKING:
    from hare.dialects.base.schema.columns.column_narrowing_check import ColumnNarrowingCheck


@dataclass(frozen=True)
class DecimalDigitsLimit(NarrowingLimit):
    """A value fits ``DECIMAL(max_digits, decimal_places)``."""

    max_digits: int
    decimal_places: int

    def get_overflow_predicate_sql(self, check: ColumnNarrowingCheck, quoted_column: str) -> str:
        return check.get_decimal_overflow_predicate_sql(quoted_column, self.max_digits, self.decimal_places)
