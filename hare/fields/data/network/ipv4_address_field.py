from __future__ import annotations

import ipaddress
from typing import Any

from hare.fields.data.network.constants import IPV4_ADDRESS_TEXT_LENGTH
from hare.fields.data.network.ip_address_field import IPAddressField


class IPv4AddressField(IPAddressField):
    """An IPv4 host address - an ``IPv4Address``; its text is taken too, an IPv6 address refused.
    A database with an IPv4 type stores it as one."""

    @property
    def SQL_TYPE(self) -> str:  # type: ignore[override]
        return f"VARCHAR({IPV4_ADDRESS_TEXT_LENGTH})"

    def get_python_type(self) -> Any:
        return ipaddress.IPv4Address

    def parse(self, value: Any) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
        address = super().parse(value)
        if not isinstance(address, ipaddress.IPv4Address):
            raise ValueError(f"{value!r} isn't an IPv4 address")
        return address
