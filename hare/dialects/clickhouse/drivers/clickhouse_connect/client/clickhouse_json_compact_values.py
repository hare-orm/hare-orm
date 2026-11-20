from __future__ import annotations

import datetime
import ipaddress
import uuid
import zoneinfo
from decimal import Decimal
from typing import Any

from hare.dialects.clickhouse.drivers.clickhouse_connect.constants import CLICKHOUSE_DECIMAL_CONTEXT
from hare.dialects.clickhouse.enums import ClickhouseTypeName
from hare.dialects.clickhouse.types.clickhouse_physical_type import ClickhousePhysicalType


class ClickhouseJsonCompactValues:
    """The values of a ``JSONCompact`` response read as the library reads the same values of a
    ``Native`` one - each by the type its column's ``meta`` names. A type with no reading of its own
    is read as the JSON value."""

    @classmethod
    def get_value(cls, type_name: str, value: Any) -> Any:
        """A value of a column of a type.

        Args:
            type_name: The type, as the server names it.
            value: The JSON value - numbers read as ``int`` and ``Decimal``.

        Returns:
            The Python value.
        """
        physical_type = ClickhousePhysicalType.from_sql(type_name)
        name, arguments = physical_type.name, physical_type.arguments
        if value is None:
            return None
        if name in {ClickhouseTypeName.NULLABLE, ClickhouseTypeName.LOW_CARDINALITY}:
            return cls.get_value(arguments[0], value)
        if name.startswith(("Int", "UInt")):
            return int(value)
        if name.startswith("Float") or name == ClickhouseTypeName.BFLOAT16:
            return float(value)
        if name.startswith(ClickhouseTypeName.DECIMAL):
            scale = int(arguments[-1]) if arguments else 0
            return Decimal(str(value)).quantize(Decimal(1).scaleb(-scale), context=CLICKHOUSE_DECIMAL_CONTEXT)
        if name == ClickhouseTypeName.BOOL:
            return bool(value)
        if name in {ClickhouseTypeName.DATE, ClickhouseTypeName.DATE32}:
            return datetime.date.fromisoformat(value)
        if name in {ClickhouseTypeName.DATETIME, ClickhouseTypeName.DATETIME64}:
            return cls.get_moment(physical_type, value)
        if name == ClickhouseTypeName.UUID:
            return uuid.UUID(value)
        if name in {ClickhouseTypeName.IPV4, ClickhouseTypeName.IPV6}:
            return ipaddress.ip_address(value)
        if name == ClickhouseTypeName.ARRAY:
            return [cls.get_value(arguments[0], item) for item in value]
        if name == ClickhouseTypeName.TUPLE:
            items = list(value.values()) if isinstance(value, dict) else value
            return tuple(
                cls.get_value(element.sql, item) for element, item in zip(physical_type.elements, items, strict=True)
            )
        if name == ClickhouseTypeName.MAP:
            return {cls.get_value(arguments[0], key): cls.get_value(arguments[1], item) for key, item in value.items()}
        return value

    @staticmethod
    def get_moment(physical_type: ClickhousePhysicalType, value: str) -> datetime.datetime:
        """A moment of a ``DateTime`` or a ``DateTime64`` column - in the column's time zone, where it
        names one.

        Args:
            physical_type: The parsed column type, including its optional timezone.
            value: The moment's text.

        Returns:
            The moment.
        """
        moment = datetime.datetime.fromisoformat(value)
        timezone = physical_type.timezone
        if timezone is not None:
            return moment.replace(tzinfo=zoneinfo.ZoneInfo(timezone))
        return moment
