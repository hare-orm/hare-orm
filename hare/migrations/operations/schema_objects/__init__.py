"""The operations on any named object a model keeps beside its table - an index, a constraint, a
trigger, a view, a dictionary, a function, a sequence, a policy, a grant - and the types of such objects."""

from __future__ import annotations

from hare.migrations.operations.schema_objects.add_schema_object import AddSchemaObject
from hare.migrations.operations.schema_objects.alter_schema_object import AlterSchemaObject
from hare.migrations.operations.schema_objects.remove_schema_object import RemoveSchemaObject
from hare.migrations.operations.schema_objects.rename_schema_object import RenameSchemaObject

__all__ = ["AddSchemaObject", "AlterSchemaObject", "RemoveSchemaObject", "RenameSchemaObject"]
