from __future__ import annotations

from typing import Any, TypeVar

from hare.fields.data.constants import INT16_MAX
from hare.fields.data.numeric.small_int_field import SmallIntField

TInt = TypeVar("TInt", int, int | None)


class PositiveSmallIntField(SmallIntField[TInt]):
    """A non-negative 16-bit integer - a ``SMALLINT`` column holding 0 to 2**15 - 1."""

    @property
    def constraints(self) -> dict[str, Any]:
        return {
            "ge": 0,
            "le": INT16_MAX,
        }
