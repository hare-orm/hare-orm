from __future__ import annotations

from hare.fields.enums import RelationType

#: The relations holding many related rows for one row.
TO_MANY_RELATION_TYPES = frozenset({RelationType.MANY_TO_MANY, RelationType.BACKWARD_FOREIGN_KEY})

#: The relations a row may have no related row of, whatever its own columns hold.
BACKWARD_RELATION_TYPES = frozenset(
    {RelationType.MANY_TO_MANY, RelationType.BACKWARD_FOREIGN_KEY, RelationType.BACKWARD_ONE_TO_ONE}
)
