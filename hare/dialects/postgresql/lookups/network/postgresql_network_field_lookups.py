from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.postgresql.enums import PostgresqlLookup
from hare.dialects.postgresql.lookups.network.postgresql_network_lookups import PostgresqlNetworkLookups

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.query.filters.lookups.field_lookup import FieldLookup


class PostgresqlNetworkFieldLookups:
    """The lookups of an ``inet``/``cidr`` field: equality, comparison, membership, ``isnull`` and
    ``range`` - no text lookups - and the subnet ones (``net_contained``, ``net_contains``, ...) taking
    an address or a network."""

    #: The generic lookups an address or a network keeps.
    KEPT_GENERIC_LOOKUPS = ("", "not", "in", "not_in", "isnull", "not_isnull", "gte", "lte", "gt", "lt", "range")

    @classmethod
    def get_lookups(cls, field: Field[object]) -> dict[str, FieldLookup]:
        """Builds every lookup of a network field.

        Args:
            field: The field.

        Returns:
            The lookups by suffix.
        """
        # Local import: the filters package imports the dialects package.
        from hare.dialects.postgresql.lookups.postgresql_value_encoders import PostgresqlValueEncoders
        from hare.query.filters.lookups.field_lookup import FieldLookup
        from hare.query.filters.lookups.field_lookups import FieldLookups

        generic = FieldLookups.get_generic(field)
        lookups = {name: generic[name] for name in cls.KEPT_GENERIC_LOOKUPS}
        for lookup_name, network_operator in (
            (PostgresqlLookup.NET_CONTAINED, PostgresqlNetworkLookups.contained),
            (PostgresqlLookup.NET_CONTAINED_OR_EQUAL, PostgresqlNetworkLookups.contained_or_equal),
            (PostgresqlLookup.NET_CONTAINS, PostgresqlNetworkLookups.contains),
            (PostgresqlLookup.NET_CONTAINS_OR_EQUAL, PostgresqlNetworkLookups.contains_or_equal),
            (PostgresqlLookup.NET_OVERLAPS, PostgresqlNetworkLookups.overlaps),
        ):
            lookups[lookup_name] = FieldLookup(network_operator, PostgresqlValueEncoders.encode_network)
        return lookups
