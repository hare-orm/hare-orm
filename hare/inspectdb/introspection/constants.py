from __future__ import annotations

from hare.inspectdb.enums import SchemaPartType

#: The parts of a catalog whose definition is SQL text - compared by their text with names left out.
CATALOG_SQL_TEXT_PARTS = frozenset(
    {SchemaPartType.INDEX, SchemaPartType.CONSTRAINT, SchemaPartType.TRIGGER, SchemaPartType.UNPARSED_PARTITIONING}
)
