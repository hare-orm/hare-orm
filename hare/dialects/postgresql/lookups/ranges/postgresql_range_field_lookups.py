from __future__ import annotations

import operator
from typing import TYPE_CHECKING

from hare.dialects.postgresql.enums import PostgresqlLookup
from hare.query.enums import Lookup

if TYPE_CHECKING:
    from hare.query.filters.lookups.field_lookup import FieldLookup
from hare.dialects.postgresql.lookups.ranges.postgresql_range_lookups import PostgresqlRangeLookups


class PostgresqlRangeFieldLookups:
    """The lookups of a range field: equality and ``in``/``not_in``, ``isnull``, containment
    (``contains``, ``contained_by``, ``overlap``) and position (``fully_lt``, ``fully_gt``, ``not_lt``,
    ``not_gt``, ``adjacent_to``).
    No text lookups - ``__contains`` tests containment.
    """

    @staticmethod
    def get_lookups() -> dict[str, FieldLookup]:
        """Builds every lookup of a range field.

        Returns:
            The lookups by suffix.
        """
        # Local import: the filters package imports the dialects package.
        from hare.dialects.postgresql.lookups.postgresql_value_encoders import PostgresqlValueEncoders
        from hare.query.filters import Lookups, ValueEncoders
        from hare.query.filters.lookups.field_lookup import FieldLookup

        lookups: dict[str, FieldLookup] = {
            Lookup.EXACT: FieldLookup(operator.eq, PostgresqlValueEncoders.encode_range),
            Lookup.NOT: FieldLookup(Lookups.not_equal, PostgresqlValueEncoders.encode_range),
            Lookup.IN: FieldLookup(Lookups.is_in, PostgresqlValueEncoders.encode_range_list),
            Lookup.NOT_IN: FieldLookup(Lookups.not_in, PostgresqlValueEncoders.encode_range_list),
            Lookup.ISNULL: FieldLookup(Lookups.is_null, ValueEncoders.encode_bool),
            Lookup.NOT_ISNULL: FieldLookup(Lookups.not_null, ValueEncoders.encode_bool),
            Lookup.CONTAINS: FieldLookup(
                PostgresqlRangeLookups.contains, PostgresqlValueEncoders.encode_range_or_element
            ),
        }
        for lookup_name, range_operator in (
            (Lookup.CONTAINED_BY, PostgresqlRangeLookups.contained_by),
            (Lookup.OVERLAP, PostgresqlRangeLookups.overlap),
            (PostgresqlLookup.FULLY_LT, PostgresqlRangeLookups.fully_lt),
            (PostgresqlLookup.FULLY_GT, PostgresqlRangeLookups.fully_gt),
            (PostgresqlLookup.NOT_LT, PostgresqlRangeLookups.not_lt),
            (PostgresqlLookup.NOT_GT, PostgresqlRangeLookups.not_gt),
            (PostgresqlLookup.ADJACENT_TO, PostgresqlRangeLookups.adjacent_to),
        ):
            lookups[lookup_name] = FieldLookup(range_operator, PostgresqlValueEncoders.encode_range)
        return lookups
