from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DecimalDigitsLimit:
    """A value fits ``DECIMAL(max_digits, decimal_places)``."""

    max_digits: int
    decimal_places: int
