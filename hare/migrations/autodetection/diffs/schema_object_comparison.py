from __future__ import annotations

from collections.abc import Hashable
from functools import cached_property
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.migrations.autodetection.diffs.schema_object_diff import SchemaObjectDiff


class SchemaObjectComparison:
    """Lazily prepared facts about one object during a single schema comparison.

    The diff's hooks retain declaration-specific behavior. Facts are evaluated only
    when matching needs them, so a branch skipping a signature does not evaluate it.
    Entries belong to one call of ``get_operations()``, never to a model or global cache.

    Args:
        diff: The comparison rules for this object's schema type.
        schema_object: The original declaration passed to migration operations.
    """

    def __init__(self, diff: SchemaObjectDiff, schema_object: Any) -> None:
        self.diff = diff
        self.schema_object = schema_object

    @cached_property
    def name(self) -> str | None:
        """The declared name, including None for an unnamed object."""
        return self.diff.get_name(self.schema_object)

    @cached_property
    def signature(self) -> Hashable:
        """The structural declaration without its name."""
        return self.diff.get_signature(self.schema_object)

    @cached_property
    def can_be_renamed(self) -> bool:
        """Whether this declaration permits a rename operation."""
        return self.diff.can_be_renamed(self.schema_object)

    @cached_property
    def reads_changed_columns(self) -> bool:
        """Whether this declaration reads a column affected by this diff."""
        return self.diff.reads_changed_columns(self.schema_object)
