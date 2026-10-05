from __future__ import annotations

from typing import Any, TypeVar

from hare.fields.data.constants import INT32_MAX
from hare.fields.data.numeric.int_field import IntField

TInt = TypeVar("TInt", int, int | None)


class PositiveIntField(IntField[TInt]):
    """A non-negative 32-bit integer - an ``INT`` column holding 0 to 2**31 - 1."""

    @property
    def constraints(self) -> dict[str, Any]:
        return {
            "ge": 0,
            "le": INT32_MAX,
        }
