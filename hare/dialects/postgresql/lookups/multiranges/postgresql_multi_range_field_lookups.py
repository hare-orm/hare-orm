from __future__ import annotations

import operator
from typing import TYPE_CHECKING

from hare.dialects.postgresql.lookups.ranges.postgresql_range_lookups import PostgresqlRangeLookups

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.filters.lookups.field_lookup import FieldLookup


class PostgresqlMultiRangeFieldLookups:
    """The lookups of a multirange field - a range field's: equality, ``isnull``, containment
    (``contains``, ``contained_by``, ``overlap``) and position (``fully_lt``, ``fully_gt``, ``not_lt``,
    ``not_gt``, ``adjacent_to``). The value is a list of ranges or one range; ``contains`` also takes
    one value. No text lookups.
    """

    @staticmethod
    def get_lookups() -> dict[str, FieldLookup]:
        """Builds every lookup of a multirange field.

        Returns:
            The lookups by suffix.
        """
        # Local import: the filters package imports the dialects package.
        from hare.dialects.postgresql.lookups.postgresql_value_encoders import PostgresqlValueEncoders
        from hare.query.filters import Lookups, ValueEncoders
        from hare.query.filters.lookups.field_lookup import FieldLookup

        lookups = {
            "": FieldLookup(operator.eq, PostgresqlValueEncoders.encode_multi_range),
            "not": FieldLookup(Lookups.not_equal, PostgresqlValueEncoders.encode_multi_range),
            "isnull": FieldLookup(Lookups.is_null, ValueEncoders.encode_bool),
            "not_isnull": FieldLookup(Lookups.not_null, ValueEncoders.encode_bool),
            "contains": FieldLookup(
                PostgresqlRangeLookups.contains, PostgresqlValueEncoders.encode_multi_range_or_element
            ),
        }
        for lookup_name, range_operator in (
            ("contained_by", PostgresqlRangeLookups.contained_by),
            ("overlap", PostgresqlRangeLookups.overlap),
            ("fully_lt", PostgresqlRangeLookups.fully_lt),
            ("fully_gt", PostgresqlRangeLookups.fully_gt),
            ("not_lt", PostgresqlRangeLookups.not_lt),
            ("not_gt", PostgresqlRangeLookups.not_gt),
            ("adjacent_to", PostgresqlRangeLookups.adjacent_to),
        ):
            lookups[lookup_name] = FieldLookup(range_operator, PostgresqlValueEncoders.encode_multi_range)
        return lookups
