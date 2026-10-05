from __future__ import annotations

import math
import struct
from typing import TYPE_CHECKING, Any, TypeVar

from hare.dialects.clickhouse.enums import ClickhouseDialectName
from hare.dialects.clickhouse.fields.constants import FLOAT32_MAX, FLOAT32_MAX_DIGITS, FLOAT32_MIN_DIGITS
from hare.exceptions import ValidationError
from hare.fields.data.numeric.float_field import FloatField

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model

TFloat = TypeVar("TFloat", float, float | None)


class Float32Field(FloatField[TFloat]):
    """``Float32`` - a single-precision float: read back as the shortest decimal that is the same
    float32 (``0.1``, not the ``0.10000000149011612`` the driver gives); a value beyond its range is
    refused."""

    SQL_TYPE = "Float32"
    SUPPORTED_DIALECTS = frozenset({ClickhouseDialectName.CLICKHOUSE})
    keeps_native_db_values = False

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Any:
        value = super().to_db_value(value, instance)
        if isinstance(value, float) and math.isfinite(value) and abs(value) > FLOAT32_MAX:
            raise ValidationError(f"{self.model_field_name}: {value!r} is beyond a float32's range")
        return value

    def from_db_value(self, value: Any) -> Any:
        if value is None:
            return None
        return self.get_shortest_float32(float(value))

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
