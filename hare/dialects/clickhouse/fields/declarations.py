from __future__ import annotations

from typing import ClassVar, TypeVar

from hare.dialects.clickhouse.fields.clickhouse_integer_field import ClickhouseIntegerField
from hare.dialects.clickhouse.fields.constants import (
    INT128_RANGE,
    INT256_RANGE,
    UINT8_RANGE,
    UINT16_RANGE,
    UINT32_RANGE,
    UINT64_RANGE,
    UINT128_RANGE,
    UINT256_RANGE,
)

TInt = TypeVar("TInt", int, int | None)


class UInt8Field(ClickhouseIntegerField[TInt]):
    """``UInt8`` - 0 to 255."""

    SQL_TYPE = "UInt8"
    COLUMN_TYPE_RANGE: ClassVar[tuple[int, int]] = UINT8_RANGE


class UInt16Field(ClickhouseIntegerField[TInt]):
    """``UInt16`` - 0 to 65 535."""

    SQL_TYPE = "UInt16"
    COLUMN_TYPE_RANGE: ClassVar[tuple[int, int]] = UINT16_RANGE


class UInt32Field(ClickhouseIntegerField[TInt]):
    """``UInt32`` - 0 to 2**32 - 1."""

    SQL_TYPE = "UInt32"
    COLUMN_TYPE_RANGE: ClassVar[tuple[int, int]] = UINT32_RANGE


class UInt64Field(ClickhouseIntegerField[TInt]):
    """``UInt64`` - 0 to 2**64 - 1."""

    SQL_TYPE = "UInt64"
    COLUMN_TYPE_RANGE: ClassVar[tuple[int, int]] = UINT64_RANGE


class UInt128Field(ClickhouseIntegerField[TInt]):
    """``UInt128`` - 0 to 2**128 - 1."""

    SQL_TYPE = "UInt128"
    COLUMN_TYPE_RANGE: ClassVar[tuple[int, int]] = UINT128_RANGE


class UInt256Field(ClickhouseIntegerField[TInt]):
    """``UInt256`` - 0 to 2**256 - 1."""

    SQL_TYPE = "UInt256"
    COLUMN_TYPE_RANGE: ClassVar[tuple[int, int]] = UINT256_RANGE


class Int128Field(ClickhouseIntegerField[TInt]):
    """``Int128`` - -2**127 to 2**127 - 1."""

    SQL_TYPE = "Int128"
    COLUMN_TYPE_RANGE: ClassVar[tuple[int, int]] = INT128_RANGE


class Int256Field(ClickhouseIntegerField[TInt]):
    """``Int256`` - -2**255 to 2**255 - 1."""

    SQL_TYPE = "Int256"
    COLUMN_TYPE_RANGE: ClassVar[tuple[int, int]] = INT256_RANGE
