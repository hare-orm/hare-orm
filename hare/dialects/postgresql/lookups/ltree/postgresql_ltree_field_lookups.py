from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.postgresql.enums import PostgresqlLookup
from hare.dialects.postgresql.lookups.ltree.postgresql_ltree_lookups import PostgresqlLtreeLookups

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.query.filters.lookups.field_lookup import FieldLookup


class PostgresqlLtreeFieldLookups:
    """The lookups of an ltree field: equality, comparison (the tree's depth-first order), membership,
    ``isnull`` and ``range`` - no text lookups - and the tree ones: ``ancestor_of``/``descendant_of``
    taking a path, ``matches``/``matches_any`` taking ``lquery`` patterns, ``matches_text`` an
    ``ltxtquery``."""

    #: The generic lookups an ltree path keeps.
    KEPT_GENERIC_LOOKUPS = ("", "not", "in", "not_in", "isnull", "not_isnull", "gte", "lte", "gt", "lt", "range")

    @classmethod
    def get_lookups(cls, field: Field[Any]) -> dict[str, FieldLookup]:
        """Builds every lookup of an ltree field.

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
        lookups[PostgresqlLookup.ANCESTOR_OF] = FieldLookup(
            PostgresqlLtreeLookups.ancestor_of, PostgresqlValueEncoders.encode_ltree
        )
        lookups[PostgresqlLookup.DESCENDANT_OF] = FieldLookup(
            PostgresqlLtreeLookups.descendant_of, PostgresqlValueEncoders.encode_ltree
        )
        lookups[PostgresqlLookup.MATCHES] = FieldLookup(
            PostgresqlLtreeLookups.matches, PostgresqlValueEncoders.encode_lquery
        )
        lookups[PostgresqlLookup.MATCHES_ANY] = FieldLookup(
            PostgresqlLtreeLookups.matches_any, PostgresqlValueEncoders.encode_lquery_list
        )
        lookups[PostgresqlLookup.MATCHES_TEXT] = FieldLookup(
            PostgresqlLtreeLookups.matches_text, PostgresqlValueEncoders.encode_ltxtquery
        )
        return lookups
