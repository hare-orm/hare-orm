from dataclasses import dataclass

from hare.inspectdb.enums import SchemaPartType


@dataclass(frozen=True)
class ObjectMismatch:
    """A physical schema difference without a faithful migration operation."""

    part_type: SchemaPartType
    table: str
    name: str
    detail: str
