from __future__ import annotations

from typing import ClassVar

from hare.migrations.operations.schema_objects.index_object_type import IndexObjectType
from hare.migrations.operations.schema_objects.rename_schema_object import RenameSchemaObject


class RenameIndex(RenameSchemaObject):
    """Renames an index of a model, given by its old name or, for an unnamed one, its fields."""

    object_type: ClassVar[type[IndexObjectType]] = IndexObjectType
