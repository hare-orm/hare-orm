from __future__ import annotations

from typing import Any, ClassVar, TypeVar

from hare.fields.data.constants import INT64_MAX, INT64_MIN
from hare.fields.data.numeric.int_field import IntField

TInt = TypeVar("TInt", int, int | None)


class BigIntField(IntField[TInt]):
    """
    Big integer field. (64-bit signed)

    ``primary_key`` (bool):
        True if field is Primary Key.
    """

    SQL_TYPE = "BIGINT"
    COLUMN_TYPE_RANGE: ClassVar[tuple[int, int]] = (INT64_MIN, INT64_MAX)

    @property
    def constraints(self) -> dict[str, Any]:
        return {
            "ge": INT64_MIN,
            "le": INT64_MAX,
        }
