from __future__ import annotations

from enum import StrEnum


class MathResultType(StrEnum):
    """How the result type of a numeric function follows its arguments."""

    #: The first argument's type.
    SAME = "same"
    #: A Decimal stays a Decimal; anything else is a float.
    NUMERIC = "numeric"
    #: As ``NUMERIC``, and integers stay integers.
    REMAINDER = "remainder"
    #: Always a float.
    FLOAT = "float"
