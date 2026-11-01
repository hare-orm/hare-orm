from __future__ import annotations

import math
import struct
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from hare.dialects.clickhouse.fields.constants import FLOAT32_MAX
from hare.exceptions import ValidationError
from hare.fields.data.numeric.float_field import FloatField
from hare.fields.enums import ValueComputation
from hare.fields.narrowing.constants import FLOAT32_MANTISSA_BITS

if TYPE_CHECKING:
    from hare.fields.field import Field
    from hare.models import Model

TFloat = TypeVar("TFloat", float, float | None)


class Float32Field(FloatField[TFloat]):
    """``Float32`` - a single-precision float: read back as the shortest decimal that is the same
    float32 (``0.1``, not the ``0.10000000149011612`` the driver gives); a value beyond its range, or
    so close to zero it would be stored as 0, is refused - a float32 holds neither."""

    SQL_TYPE = "Float32"
    MANTISSA_BITS = FLOAT32_MANTISSA_BITS
    EXACT_DECIMAL_DIGITS = 6
    COLUMN_TYPE_FROM_DIALECT = True
    keeps_native_db_values = False

    #: The field of a sum, an average and arithmetic of float32 values - ClickHouse computes each as a
    #: Float64, which a float32 would round.
    FLOAT64_RESULT_FIELD: ClassVar[FloatField[float]] = FloatField()

    def get_computed_field(self, computation: ValueComputation) -> Field[Any]:
        if computation is ValueComputation.STATISTIC:
            # A standard deviation or a variance of float32 values is a Float32.
            return self
        return Float32Field.FLOAT64_RESULT_FIELD

    def to_db_value(self, value: Any, instance: type[Model] | Model | None) -> Any:
        value = super().to_db_value(value, instance)
        if isinstance(value, float) and math.isfinite(value) and abs(value) > FLOAT32_MAX:
            raise ValidationError(f"{self.model_field_name}: {value!r} is beyond a float32's range")
        if isinstance(value, float) and value != 0 and struct.unpack("f", struct.pack("f", value))[0] == 0:
            raise ValidationError(
                f"{self.model_field_name}: {value!r} is too close to zero for a float32 - it would be stored as 0"
            )
        return value

    def from_db_value(self, value: Any) -> Any:
        if value is None:
            return None
        return FloatField.get_shortest_float32(float(value))
