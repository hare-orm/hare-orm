from __future__ import annotations

import ipaddress
from typing import Any

from hare.dialects.postgresql.fields.constants import POSTGRESQL_CIDR_TYPE
from hare.dialects.postgresql.fields.network.network_field import NetworkField


class CidrField(NetworkField):
    """An IPv4 or IPv6 network - a ``cidr`` column. A value is read as an ``IPv4Network``/
    ``IPv6Network``; text and ``ipaddress`` objects are taken - an address as the network of it alone
    (``/32``). A network with host bits set (``10.0.0.1/8``) raises ``ValidationError``, as the column
    refuses it.
    """

    SQL_TYPE = POSTGRESQL_CIDR_TYPE
    field_type = str

    def get_python_type(self) -> Any:
        return ipaddress.IPv4Network | ipaddress.IPv6Network

    def parse(self, value: Any) -> Any:
        return ipaddress.ip_network(str(value), strict=True)
