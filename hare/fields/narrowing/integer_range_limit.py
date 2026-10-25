from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hare.fields.narrowing.narrowing_limit import NarrowingLimit

if TYPE_CHECKING:
    from hare.dialects.base.schema.columns.column_narrowing_check import ColumnNarrowingCheck


@dataclass(frozen=True)
class IntegerRangeLimit(NarrowingLimit):
    """A value is a whole number from ``lowest`` to ``highest``."""

    lowest: int
    highest: int
    #: Whether the old column could hold a fraction - a float or a decimal.
    checks_fraction: bool

    def get_overflow_predicate_sql(self, check: ColumnNarrowingCheck, quoted_column: str) -> str:
        return check.get_integer_overflow_predicate_sql(self, quoted_column)
