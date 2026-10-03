from __future__ import annotations

from typing import Any, TypeVar

from hare.fields.constants import (
    INT16_MAX,
)
from hare.fields.data.numeric.small_int_field import SmallIntField

TInt = TypeVar("TInt", int, int | None)


class PositiveSmallIntField(SmallIntField[TInt]):
    """
    Positive small integer field. (16-bit signed, >= 0)

    ``primary_key`` (bool):
        True if field is Primary Key.
    """

    @property
    def constraints(self) -> dict[str, Any]:
        return {
            "ge": 0,
            "le": INT16_MAX,
        }
