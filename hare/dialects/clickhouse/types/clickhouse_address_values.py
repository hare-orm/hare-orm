from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.data.network.ip_address_field import IPAddressField
    from hare.fields.field import Field
    from hare.models import Model


class ClickhouseAddressValues:
    """How ClickHouse writes addresses - an ``IPv6`` column takes an IPv4 address mapped into IPv6
    (``::ffff:1.2.3.4``), an ``IPv4`` column the IPv4 address itself."""

    @staticmethod
    def to_db(field: Field[Any], value: Any, instance: type[Model] | Model | None) -> Any:
        """The address as the column takes it.

        Args:
            field: The address field.
            value: The Python value.
            instance: The model (class) it is written or compared for.

        Returns:
            The address, None for None.
        """
        address_field = cast("IPAddressField", field)
        text = address_field.to_db_value(value, instance)  # type: ignore[arg-type]
        if text is None:
            return None
        address = ipaddress.ip_address(text)
        if isinstance(address, ipaddress.IPv4Address) and address_field.get_python_type() is not ipaddress.IPv4Address:
            return ipaddress.IPv6Address(f"::ffff:{address}")
        return address
