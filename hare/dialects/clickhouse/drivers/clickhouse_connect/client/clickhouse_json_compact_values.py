from __future__ import annotations

import datetime
import ipaddress
import uuid
import zoneinfo
from decimal import Decimal
from typing import Any

from hare.dialects.clickhouse.drivers.clickhouse_connect.constants import CLICKHOUSE_DECIMAL_CONTEXT
from hare.dialects.clickhouse.types.clickhouse_type_names import ClickhouseTypeNames


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
        name, arguments = ClickhouseTypeNames.get_type_parts(type_name)
        if value is None:
            return None
        if name in {"Nullable", "LowCardinality"}:
            return cls.get_value(arguments[0], value)
        if name.startswith(("Int", "UInt")):
            return int(value)
        if name.startswith("Float") or name == "BFloat16":
            return float(value)
        if name.startswith("Decimal"):
            scale = int(arguments[-1]) if arguments else 0
            return Decimal(str(value)).quantize(Decimal(1).scaleb(-scale), context=CLICKHOUSE_DECIMAL_CONTEXT)
        if name == "Bool":
            return bool(value)
        if name in {"Date", "Date32"}:
            return datetime.date.fromisoformat(value)
        if name in {"DateTime", "DateTime64"}:
            return cls.get_moment(name, arguments, value)
        if name == "UUID":
            return uuid.UUID(value)
        if name in {"IPv4", "IPv6"}:
            return ipaddress.ip_address(value)
        if name == "Array":
            return [cls.get_value(arguments[0], item) for item in value]
        if name == "Tuple":
            items = list(value.values()) if isinstance(value, dict) else value
            return tuple(
                cls.get_value(cls.get_element_type(argument), item)
                for argument, item in zip(arguments, items, strict=True)
            )
        if name == "Map":
            return {cls.get_value(arguments[0], key): cls.get_value(arguments[1], item) for key, item in value.items()}
        return value

    @staticmethod
    def get_element_type(argument: str) -> str:
        """The type of a tuple's element - a named element's type after its name.

        Args:
            argument: The element's declaration.

        Returns:
            The type.
        """
        name, _arguments = ClickhouseTypeNames.get_type_parts(argument)
        if " " in name.strip():
            return argument.strip().split(" ", 1)[1]
        return argument

    @staticmethod
    def get_moment(name: str, arguments: list[str], value: str) -> datetime.datetime:
        """A moment of a ``DateTime`` or a ``DateTime64`` column - in the column's time zone, where it
        names one.

        Args:
            name: The type's name.
            arguments: Its arguments - a precision of a ``DateTime64`` first, a zone last.
            value: The moment's text.

        Returns:
            The moment.
        """
        moment = datetime.datetime.fromisoformat(value)
        zone_arguments = arguments[1:] if name == "DateTime64" else arguments
        if zone_arguments:
            return moment.replace(tzinfo=zoneinfo.ZoneInfo(zone_arguments[0].strip().strip("'")))
        return moment
