from __future__ import annotations

from typing import Any, TypeVar

from hare.fields.data.constants import INT64_MAX
from hare.fields.data.numeric.big_int_field import BigIntField

TInt = TypeVar("TInt", int, int | None)


class PositiveBigIntField(BigIntField[TInt]):
    """A non-negative 64-bit integer - a ``BIGINT`` column holding 0 to 2**63 - 1."""

    @property
    def constraints(self) -> dict[str, Any]:
        return {
            "ge": 0,
            "le": INT64_MAX,
        }
