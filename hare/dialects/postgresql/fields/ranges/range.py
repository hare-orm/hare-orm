from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Range(Generic[T]):
    """A range value - what every ``RangeField`` holds in Python, whatever the driver.

    Args:
        lower: The lower bound, None for unbounded.
        upper: The upper bound, None for unbounded.
        lower_inc: Whether the lower bound is included - True by default.
        upper_inc: Whether the upper bound is included - False by default.
        is_empty: Whether this is the empty range (``'empty'``), which holds no value - unlike an
            unbounded range, which has the same None bounds.
    """

    lower: T | None = None
    upper: T | None = None
    lower_inc: bool = True
    upper_inc: bool = False
    is_empty: bool = False
