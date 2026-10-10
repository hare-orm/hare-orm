from __future__ import annotations

from typing import Any, ClassVar, TypeVar

from hare.fields.data.constants import INT16_MAX, INT16_MIN
from hare.fields.data.numeric.int_field import IntField

TInt = TypeVar("TInt", int, int | None)


class SmallIntField(IntField[TInt]):
    """
    Small integer field. (16-bit signed)

    ``primary_key`` (bool):
        True if field is Primary Key.
    """

    SQL_TYPE = "SMALLINT"
    COLUMN_TYPE_RANGE: ClassVar[tuple[int, int]] = (INT16_MIN, INT16_MAX)

    @property
    def constraints(self) -> dict[str, Any]:
        return {
            "ge": INT16_MIN,
            "le": INT16_MAX,
        }
