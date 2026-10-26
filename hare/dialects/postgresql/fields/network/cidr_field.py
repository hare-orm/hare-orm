from __future__ import annotations

import ipaddress
from typing import Any

from hare.dialects.postgresql.fields.constants import POSTGRESQL_CIDR_TYPE
from hare.dialects.postgresql.fields.network.inet_field import InetField
from hare.dialects.postgresql.fields.network.network_field import NetworkField
from hare.dialects.postgresql.fields.network.network_limit import NetworkLimit
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField
from hare.fields.narrowing.narrowing_limit import NarrowingLimit


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

    def get_accepted_value_types(self) -> tuple[Any, ...]:
        return (ipaddress.IPv4Network, ipaddress.IPv6Network, ipaddress.IPv4Address, ipaddress.IPv6Address)

    def parse(self, value: Any) -> Any:
        return ipaddress.ip_network(str(value), strict=True)

    def get_narrowing_limit(self, old_field: Field[Any]) -> NarrowingLimit | None:
        """An address column's values fit a network column only with no host bits set - PostgreSQL's
        conversion would drop them (``10.0.0.1/8`` becomes ``10.0.0.0/8``)."""
        if isinstance(GeneratedField.get_effective_field(old_field), InetField):
            return NetworkLimit()
        return super().get_narrowing_limit(old_field)
