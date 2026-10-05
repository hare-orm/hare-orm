from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from hare.dialects.postgresql.fields.ranges.range_field import RangeField
from hare.fields import Field
from hare.query.rows.enums import RangeBoundType


class DecimalRangeField(RangeField):
    """``numrange`` - a range of arbitrary-precision decimals."""

    SQL_TYPE = "numrange"
    ELEMENT_SQL_TYPE = "numeric"
    NATIVE_BOUND_TYPE = RangeBoundType.DECIMAL

    def get_bound_field(self) -> Field[Any]:
        from hare.query.expressions.numeric.numeric_typing import NumericTyping

        return NumericTyping.DECIMAL_QUOTIENT_OUTPUT_FIELD  # type: ignore[arg-type]

    def parse_bound_text(self, text: str) -> Any:
        return Decimal(text)

    def coerce_bound(self, value: Any) -> Any:
        if value is None or isinstance(value, Decimal):
            return value
        try:
            return Decimal(value)
        except (InvalidOperation, ValueError, TypeError) as error:
            validation_error = self.get_validation_error(error, value)
        raise validation_error
