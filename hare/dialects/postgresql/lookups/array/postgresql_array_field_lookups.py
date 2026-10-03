from __future__ import annotations

import operator
from functools import partial
from typing import TYPE_CHECKING

from hare.dialects.postgresql.fields.array import ArrayField

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.filters.field_lookup import FieldLookup
from hare.dialects.postgresql.lookups.array.postgresql_array_lookups import PostgresqlArrayLookups


class PostgresqlArrayFieldLookups:
    """The lookups of an ``ArrayField``: equality of the whole array, ``isnull``, containment
    (``contains``, ``contained_by``, ``overlap``), ``len`` and ``item``."""

    @staticmethod
    def get_lookups(field: ArrayField) -> dict[str, FieldLookup]:
        """Builds every lookup of an array field.

        Args:
            field: The array field.

        Returns:
            The lookups by suffix.
        """
        # Local import: the filters package imports the dialects package.
        from hare.dialects.postgresql.lookups.encoders import PostgresqlValueEncoders
        from hare.query.filters import Lookups, ValueEncoders
        from hare.query.filters.field_lookup import FieldLookup

        return {
            "": FieldLookup(operator.eq, PostgresqlValueEncoders.encode_array),
            "not": FieldLookup(Lookups.not_equal, PostgresqlValueEncoders.encode_array),
            "isnull": FieldLookup(Lookups.is_null, ValueEncoders.encode_bool),
            "not_isnull": FieldLookup(Lookups.not_null, ValueEncoders.encode_bool),
            "contains": FieldLookup(PostgresqlArrayLookups.contains, PostgresqlValueEncoders.encode_array),
            "contained_by": FieldLookup(PostgresqlArrayLookups.contained_by, PostgresqlValueEncoders.encode_array),
            "overlap": FieldLookup(PostgresqlArrayLookups.overlap, PostgresqlValueEncoders.encode_array),
            "len": FieldLookup(PostgresqlArrayLookups.length, ValueEncoders.encode_int),
            "item": FieldLookup(
                partial(PostgresqlArrayLookups.item, element_field=field.base_field),
                PostgresqlValueEncoders.encode_array_item,
            ),
        }
