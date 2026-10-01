from __future__ import annotations

from typing import Any, TypeVar

from hare.fields.constants import (
    INT64_MAX,
)
from hare.fields.data.numeric.big_int_field import BigIntField

TInt = TypeVar("TInt", int, int | None)


class PositiveBigIntField(BigIntField[TInt]):
    """
    Positive big integer field. (64-bit signed, >= 0)

    ``primary_key`` (bool):
        True if field is Primary Key.
    """

    @property
    def constraints(self) -> dict[str, Any]:
        return {
            "ge": 0,
            "le": INT64_MAX,
        }
