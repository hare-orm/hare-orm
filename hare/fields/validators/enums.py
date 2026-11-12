from __future__ import annotations

from enum import StrEnum


class DigitsFailure(StrEnum):
    """Why a ``Decimal`` fails ``MaxDigitsValidator`` - the reason its native check gives."""

    NOT_FINITE = "not_finite"
    DIGITS = "digits"
    DECIMAL_PLACES = "decimal_places"
    WHOLE_DIGITS = "whole_digits"
