from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from hare.dialects.postgresql.enums import RangeBoundType
from hare.dialects.postgresql.fields.ranges.range import Range
from hare.dialects.postgresql.fields.ranges.range_field import RangeField
from hare.exceptions import ValidationError
from hare.fields import Field


class DecimalRangeField(RangeField):
    """``numrange`` - a range of arbitrary-precision decimals."""

    SQL_TYPE = "numrange"
    ELEMENT_SQL_TYPE = "numeric"
    NATIVE_BOUND_TYPE = RangeBoundType.DECIMAL

    def get_bound_field(self) -> Field[Any]:
        from hare.query.expressions.numeric.numeric_typing import NumericTyping

        return NumericTyping.DECIMAL_QUOTIENT_OUTPUT_FIELD

    def get_value_annotation(self) -> Any:
        """A range of decimals - an infinite bound too, as the column holds it and a schema writes it."""
        # Imported here: only a schema reads the annotation, and pydantic is an extra.
        from pydantic import AllowInfNan

        return Range[Annotated[Decimal, AllowInfNan(True)]]

    def parse_bound_text(self, text: str) -> Any:
        return Decimal(text)

    def coerce_bound(self, value: Any) -> Any:
        if value is None:
            return value
        if not isinstance(value, Decimal):
            validation_error = None
            try:
                value = Decimal(value)
            except (InvalidOperation, ValueError, TypeError) as error:
                validation_error = self.get_validation_error(error, value)
            if validation_error is not None:
                # Raised outside the except block - a sensitive field's error keeps no context.
                raise validation_error
        if value.is_nan():
            # A NaN bound is ordered against no other - comparing the bounds raises.
            raise ValidationError(f"{self.model_field_name}: a range bound can't be NaN")
        return value
