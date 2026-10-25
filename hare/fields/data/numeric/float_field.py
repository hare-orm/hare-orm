from __future__ import annotations

import math
import struct
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar, Literal, TypeVar, overload

from hare.exceptions import ValidationError
from hare.fields.data.constants import FLOAT32_MAX_DIGITS, FLOAT32_MIN_DIGITS
from hare.fields.enums import NativeWriteCheck
from hare.fields.field import Field
from hare.fields.narrowing.float_precision_limit import FloatPrecisionLimit

if TYPE_CHECKING:
    from hare.fields.narrowing.narrowing_limit import NarrowingLimit
    from hare.models import Model

TFloat = TypeVar("TFloat", float, float | None)


class FloatField(Field[TFloat]):
    """A 64-bit floating point number - a ``DOUBLE PRECISION`` column. Takes an int, a float or a
    ``Decimal`` a float holds; a bool or a text is refused."""

    field_type = float

    SQL_TYPE = "DOUBLE PRECISION"
    #: The bits of a value's mantissa - an integer past two to their power isn't held exactly.
    MANTISSA_BITS: ClassVar[int] = 53
    #: The significant digits of which every decimal is held back exactly.
    EXACT_DECIMAL_DIGITS: ClassVar[int] = 15

    def get_narrowing_limit(self, old_field: Field[Any]) -> NarrowingLimit | None:
        # Imported here: the modules import each other.
        from hare.fields.data.numeric.decimal_field import DecimalField
        from hare.fields.data.numeric.int_field import IntField

        decimal_places = None
        if isinstance(old_field, FloatField):
            holds_exactly = old_field.MANTISSA_BITS <= self.MANTISSA_BITS
        elif isinstance(old_field, IntField):
            holds_exactly = max(map(abs, old_field.COLUMN_TYPE_RANGE)) <= 2**self.MANTISSA_BITS
        elif isinstance(old_field, DecimalField):
            holds_exactly = old_field.max_digits <= self.EXACT_DECIMAL_DIGITS
            decimal_places = old_field.decimal_places
        else:
            return None
        # A number the float rounds would be lost.
        return None if holds_exactly else FloatPrecisionLimit(self.MANTISSA_BITS, decimal_places)

    @staticmethod
    def get_shortest_float32(value: float) -> float:
        """The float with the shortest decimal text that is the same float32 as ``value``.

        Args:
            value: A float32 widened to a float.

        Returns:
            The float.
        """
        if not math.isfinite(value):
            return value
        packed = struct.pack("f", value)
        for digits in range(FLOAT32_MIN_DIGITS, FLOAT32_MAX_DIGITS + 1):
            candidate = float(f"{value:.{digits}g}")
            if struct.pack("f", candidate) == packed:
                return candidate
        return value

    @overload
    def __init__(self: FloatField[float], *, null: Literal[False] = False, **kwargs: Any) -> None: ...

    @overload
    def __init__(self: FloatField[float | None], *, null: Literal[True], **kwargs: Any) -> None: ...

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    native_write_check = NativeWriteCheck.NOT_NAN
    #: A float the driver returns is the value - to_python() only checks what is assigned.
    keeps_native_db_values = True

    def to_db_value(self, value: Any, instance: type[Model] | Model | None) -> Any:
        if type(value) is not float and value is not None:
            self.raise_if_not_number(value)
        return super().to_db_value(value, instance)

    def raise_if_not_number(self, value: Any) -> None:
        """Refuses a value that is no number - a bool, a text: float() would read "1_0" or "nan" as
        numbers the caller didn't mean. A Decimal is a number - a numeric expression reads as one.

        Args:
            value: The value, not None.

        Raises:
            ValidationError: ``value`` is no int, float or Decimal, or is a finite Decimal no float holds.
        """
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise ValidationError(f"{self.model_field_name}: expected a number, got {type(value).__name__}")
        # float() makes a finite Decimal past the float range an infinity - an int there raises.
        if isinstance(value, Decimal) and value.is_finite() and math.isinf(float(value)):
            raise ValidationError(f"{self.model_field_name}: Decimal too large to convert to float")

    def to_python(self, value: Any) -> Any:
        if type(value) is not float and value is not None:
            self.raise_if_not_number(value)
        return super().to_python(value)
