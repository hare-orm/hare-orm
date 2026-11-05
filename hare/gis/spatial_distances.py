from __future__ import annotations

from typing import Any

from hare.exceptions import ValidationError
from hare.numbers.finite_numbers import FiniteNumbers


class SpatialDistances:
    """Validation shared by distance predicates and their expression forms."""

    @staticmethod
    def get_value(value: Any, name: str) -> float:
        """Accept a finite nonnegative radius, preserving the geometry's distance units."""
        if not FiniteNumbers.is_finite_number(value) or value < 0:
            raise ValidationError(f"{name}: a distance is a finite number of zero or more, got {value!r}")
        return float(value)
