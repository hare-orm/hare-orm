from __future__ import annotations

from decimal import Decimal
from typing import Any

from hare.fields.data.numeric.decimal_field import DecimalField


class UnroundedDecimalField(DecimalField[Decimal]):
    """Decodes an arithmetic result to Decimal without rounding it to a fixed scale."""

    def to_python(self, value: Any) -> Decimal | None:
        if value is None or isinstance(value, Decimal):
            return value
        # repr() of a float is its shortest round-tripping text - no binary-expansion noise.
        return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
