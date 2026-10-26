from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hare.fields.narrowing.narrowing_limit import NarrowingLimit

if TYPE_CHECKING:
    from hare.dialects.base.schema.columns.column_narrowing_check import ColumnNarrowingCheck


@dataclass(frozen=True)
class FloatPrecisionLimit(NarrowingLimit):
    """A value is a float of ``mantissa_bits`` exactly - an integer or a decimal it rounds is lost.
    ``decimal_places`` are the old column's where it held decimals, None for any other number."""

    mantissa_bits: int
    decimal_places: int | None

    def get_overflow_predicate_sql(self, check: ColumnNarrowingCheck, quoted_column: str) -> str:
        return check.get_float_overflow_predicate_sql(self, quoted_column)
