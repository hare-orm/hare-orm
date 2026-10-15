from __future__ import annotations

from typing import ClassVar

from hare.migrations.operations.schema_objects.object_types.constraint_object_type import ConstraintObjectType
from hare.migrations.operations.schema_objects.remove_schema_object import RemoveSchemaObject


class RemoveConstraint(RemoveSchemaObject):
    """Removes a constraint from a model, given by its name or, for an unnamed unique one, its
    fields."""

    object_type: ClassVar[type[ConstraintObjectType]] = ConstraintObjectType
