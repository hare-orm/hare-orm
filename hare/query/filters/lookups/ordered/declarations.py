from __future__ import annotations

from hare.query.filters.lookups.ordered.ordered_value_lookups import OrderedValueLookups


class UuidOrderLookups(OrderedValueLookups):
    """The comparisons of a ``UUIDField`` - by the UUID's 16 bytes, as PostgreSQL orders them."""


class TimeOrderLookups(OrderedValueLookups):
    """The comparisons of a ``TimeField`` - by the UTC time, then by the offset, as PostgreSQL orders
    ``TIMETZ``."""


class AddressOrderLookups(OrderedValueLookups):
    """The comparisons of an ``IPAddressField`` - IPv4 before IPv6, as PostgreSQL orders ``inet``."""
