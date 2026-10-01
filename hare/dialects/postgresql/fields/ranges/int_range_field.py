from __future__ import annotations

from typing import Any

from hare.dialects.postgresql.fields.ranges.range_field import RangeField
from hare.fields import Field
from hare.query.rows.enums import RangeBoundKind


class IntRangeField(RangeField):
    """``int4range`` - a range of 32-bit integers."""

    SQL_TYPE = "int4range"
    ELEMENT_SQL_TYPE = "integer"

    def get_bound_field(self) -> Field[Any]:
        from hare.query.expressions.numeric.numeric_typing import NumericTyping

        return NumericTyping.INTEGER_OUTPUT_FIELD  # type: ignore[arg-type]

    DISCRETE_STEP = 1
    NATIVE_BOUND_KIND = RangeBoundKind.INTEGER

    def coerce_bound(self, value: Any) -> Any:
        if value is None or isinstance(value, int):
            return value
        try:
            return int(value)
        except (ValueError, TypeError) as exc:
            validation_error = self.get_validation_error(exc, value)
        raise validation_error

    def parse_bound_text(self, text: str) -> Any:
        return int(text)


class BigIntRangeField(IntRangeField):
    """``int8range`` - a range of 64-bit integers."""

    SQL_TYPE = "int8range"
    ELEMENT_SQL_TYPE = "bigint"
