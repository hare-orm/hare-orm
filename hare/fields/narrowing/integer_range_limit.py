from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IntegerRangeLimit:
    """A value is a whole number from ``lowest`` to ``highest``."""

    lowest: int
    highest: int
    #: Whether the old column could hold a fraction - a float or a decimal.
    checks_fraction: bool
