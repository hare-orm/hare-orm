from __future__ import annotations

import ipaddress
from typing import Any

from hare.dialects.postgresql.fields.constants import POSTGRESQL_INET_TYPE
from hare.dialects.postgresql.fields.network.network_field import NetworkField


class InetField(NetworkField):
    """An IPv4 or IPv6 host address, with the prefix length of its network when it has one - an
    ``inet`` column. A value is read as an ``IPv4Address``/``IPv6Address``, or as an
    ``IPv4Interface``/``IPv6Interface`` (address and network) when its prefix is shorter than the full
    length; text, those objects and an ``IPv4Network``/``IPv6Network`` are taken.
    """

    SQL_TYPE = POSTGRESQL_INET_TYPE
    field_type = str

    def get_python_type(self) -> Any:
        return ipaddress.IPv4Address | ipaddress.IPv6Address | ipaddress.IPv4Interface | ipaddress.IPv6Interface

    def parse(self, value: Any) -> Any:
        interface = ipaddress.ip_interface(str(value))
        return interface.ip if interface.network.prefixlen == interface.max_prefixlen else interface
