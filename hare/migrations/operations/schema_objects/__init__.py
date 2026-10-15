"""The operations on any named object a model keeps beside its table - an index, a constraint, a
trigger, a view, a dictionary, a function, a sequence, a policy, a grant - and the types of such objects."""

from __future__ import annotations

from hare.migrations.operations.schema_objects.add_schema_object import AddSchemaObject
from hare.migrations.operations.schema_objects.alter_schema_object import AlterSchemaObject
from hare.migrations.operations.schema_objects.object_types.constraint_object_type import ConstraintObjectType
from hare.migrations.operations.schema_objects.object_types.dictionary_object_type import DictionaryObjectType
from hare.migrations.operations.schema_objects.object_types.function_object_type import FunctionObjectType
from hare.migrations.operations.schema_objects.object_types.grant_object_type import GrantObjectType
from hare.migrations.operations.schema_objects.object_types.index_object_type import IndexObjectType
from hare.migrations.operations.schema_objects.object_types.materialized_view_object_type import (
    MaterializedViewObjectType,
)
from hare.migrations.operations.schema_objects.object_types.policy_object_type import PolicyObjectType
from hare.migrations.operations.schema_objects.object_types.schema_object_type import SchemaObjectType
from hare.migrations.operations.schema_objects.object_types.sequence_object_type import SequenceObjectType
from hare.migrations.operations.schema_objects.object_types.trigger_object_type import TriggerObjectType
from hare.migrations.operations.schema_objects.object_types.view_object_type import ViewObjectType
from hare.migrations.operations.schema_objects.remove_schema_object import RemoveSchemaObject
from hare.migrations.operations.schema_objects.rename_schema_object import RenameSchemaObject

__all__ = [
    "AddSchemaObject",
    "AlterSchemaObject",
    "ConstraintObjectType",
    "DictionaryObjectType",
    "FunctionObjectType",
    "GrantObjectType",
    "IndexObjectType",
    "MaterializedViewObjectType",
    "PolicyObjectType",
    "RemoveSchemaObject",
    "RenameSchemaObject",
    "SchemaObjectType",
    "SequenceObjectType",
    "TriggerObjectType",
    "ViewObjectType",
]
