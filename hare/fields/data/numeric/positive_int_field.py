from __future__ import annotations

from typing import Any, TypeVar

from hare.fields.data.constants import INT32_MAX
from hare.fields.data.numeric.int_field import IntField

TInt = TypeVar("TInt", int, int | None)


class PositiveIntField(IntField[TInt]):
    """
    Positive integer field. (32-bit signed, >= 0)

    ``primary_key`` (bool):
        True if field is Primary Key.
    """

    @property
    def constraints(self) -> dict[str, Any]:
        return {
            "ge": 0,
            "le": INT32_MAX,
        }
