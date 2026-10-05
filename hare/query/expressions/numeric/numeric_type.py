from __future__ import annotations

from dataclasses import dataclass

from hare.query.expressions.enums import NumericValueType


@dataclass(frozen=True)
class NumericType:
    """The type of number a value is.

    Attributes:
        type: Integer, float or Decimal.
        scale: Digits after the decimal point of a Decimal, None when unknown.
    """

    type: NumericValueType
    scale: int | None = None
