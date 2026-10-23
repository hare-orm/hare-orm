from __future__ import annotations

from decimal import Decimal
from typing import Any

from hare.dialects.postgresql.fields.ranges.range_field import RangeField
from hare.exceptions import ValidationError
from hare.fields import Field
from hare.fields.data.constants import INT32_MAX, INT32_MIN
from hare.fields.data.numeric.int_field import IntField
from hare.query.rows.enums import RangeBoundType


class IntRangeField(RangeField):
    """``int4range`` - a range of 32-bit integers."""

    SQL_TYPE = "int4range"
    ELEMENT_SQL_TYPE = "integer"

    def get_bound_field(self) -> Field[Any]:
        from hare.query.expressions.numeric.numeric_typing import NumericTyping

        return NumericTyping.INTEGER_OUTPUT_FIELD

    DISCRETE_STEP = 1
    NATIVE_BOUND_TYPE = RangeBoundType.INTEGER
    #: The values a bound holds - those of the element type.
    BOUND_RANGE: tuple[int, int] = (INT32_MIN, INT32_MAX)

    def coerce_bound(self, value: Any) -> Any:
        if value is None:
            return value
        if isinstance(value, bool):
            # An IntField refuses a bool too - int() would write it as 0 or 1, and read an element
            # compared with the range (``__contains=True``) so.
            raise ValidationError(f"{self.model_field_name}: range bound: expected an int, got bool")
        if isinstance(value, (float, Decimal)) and not IntField.is_whole_number(value):
            # int() would cut the fraction off.
            raise ValidationError(
                f"{self.model_field_name}: range bound {self.get_value_for_message(value)} is not a whole number"
            )
        if not isinstance(value, int) or isinstance(value, bool):
            validation_error = None
            try:
                value = int(value)
            except (ValueError, TypeError) as error:
                validation_error = self.get_validation_error(error, value)
            if validation_error is not None:
                # Raised outside the except block - a sensitive field's error keeps no context.
                raise validation_error
        lowest, highest = self.BOUND_RANGE
        if not lowest <= value <= highest:
            raise ValidationError(
                f"{self.model_field_name}: range bound {self.get_value_for_message(value)} is out of "
                f"{self.ELEMENT_SQL_TYPE}'s range {lowest}..{highest}"
            )
        return value

    def parse_bound_text(self, text: str) -> Any:
        return int(text)
