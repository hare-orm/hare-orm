from __future__ import annotations

import math
from typing import Any


class FiniteNumbers:
    """Tells a finite number - one a float can hold - from anything else a setting or an argument
    may be given."""

    @staticmethod
    def is_finite_number(value: Any) -> bool:
        """Whether a value is an int or float a float can hold - not a bool, NaN, an infinity, or
        an int beyond the float range.

        Args:
            value: The value.

        Returns:
            True for such a number.
        """
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            return False
