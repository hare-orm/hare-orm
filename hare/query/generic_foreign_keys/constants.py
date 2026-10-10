from __future__ import annotations

from hare.query.enums import Lookup

#: The part of a filter key after a GenericForeignKeyField reading the name of its branch set -
#: ``target__type``.
GENERIC_FOREIGN_KEY_TYPE_KEY = "type"

#: The lookups a GenericForeignKeyField and its type take.
GENERIC_FOREIGN_KEY_LOOKUPS = frozenset(
    {Lookup.EXACT, Lookup.NOT, Lookup.IN, Lookup.NOT_IN, Lookup.ISNULL, Lookup.NOT_ISNULL}
)

#: The lookups of a GenericForeignKeyField matching the rows its condition does not.
GENERIC_FOREIGN_KEY_NEGATED_LOOKUPS = frozenset({Lookup.NOT, Lookup.NOT_IN})

#: The lookups of a GenericForeignKeyField taking a list.
GENERIC_FOREIGN_KEY_LIST_LOOKUPS = frozenset({Lookup.IN, Lookup.NOT_IN})

#: The lookups of a GenericForeignKeyField asking whether a branch is set.
GENERIC_FOREIGN_KEY_NULL_LOOKUPS = frozenset({Lookup.ISNULL, Lookup.NOT_ISNULL})
