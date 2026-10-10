from __future__ import annotations

from typing import ClassVar

from hare.migrations.autodetection.diffs.declared_schema_object_diff import DeclaredSchemaObjectDiff
from hare.migrations.operations import (
    AddDictionary,
    AddFunction,
    AddMaterializedView,
    AddPolicy,
    AddSequence,
    AddView,
    AlterDictionary,
    AlterFunction,
    AlterMaterializedView,
    AlterPolicy,
    AlterSequence,
    AlterView,
    HareOperation,
    RemoveDictionary,
    RemoveFunction,
    RemoveMaterializedView,
    RemovePolicy,
    RemoveSequence,
    RemoveView,
    RenameDictionary,
    RenameFunction,
    RenameMaterializedView,
    RenamePolicy,
    RenameSequence,
    RenameView,
)
from hare.migrations.operations.schema_objects import (
    DictionaryObjectType,
    FunctionObjectType,
    MaterializedViewObjectType,
    PolicyObjectType,
    SchemaObjectType,
    SequenceObjectType,
    ViewObjectType,
)


class ViewDiff(DeclaredSchemaObjectDiff):
    """The view operations of a model."""

    object_type: ClassVar[type[SchemaObjectType]] = ViewObjectType
    add_operation_class: ClassVar[type[HareOperation]] = AddView
    alter_operation_class: ClassVar[type[HareOperation] | None] = AlterView
    remove_operation_class: ClassVar[type[HareOperation]] = RemoveView
    rename_operation_class: ClassVar[type[HareOperation] | None] = RenameView


class MaterializedViewDiff(DeclaredSchemaObjectDiff):
    """The materialized view operations of a model."""

    object_type: ClassVar[type[SchemaObjectType]] = MaterializedViewObjectType
    add_operation_class: ClassVar[type[HareOperation]] = AddMaterializedView
    alter_operation_class: ClassVar[type[HareOperation] | None] = AlterMaterializedView
    remove_operation_class: ClassVar[type[HareOperation]] = RemoveMaterializedView
    rename_operation_class: ClassVar[type[HareOperation] | None] = RenameMaterializedView


class DictionaryDiff(DeclaredSchemaObjectDiff):
    """The dictionary operations of a model."""

    object_type: ClassVar[type[SchemaObjectType]] = DictionaryObjectType
    add_operation_class: ClassVar[type[HareOperation]] = AddDictionary
    alter_operation_class: ClassVar[type[HareOperation] | None] = AlterDictionary
    remove_operation_class: ClassVar[type[HareOperation]] = RemoveDictionary
    rename_operation_class: ClassVar[type[HareOperation] | None] = RenameDictionary


class FunctionDiff(DeclaredSchemaObjectDiff):
    """The function operations of a model."""

    object_type: ClassVar[type[SchemaObjectType]] = FunctionObjectType
    add_operation_class: ClassVar[type[HareOperation]] = AddFunction
    alter_operation_class: ClassVar[type[HareOperation] | None] = AlterFunction
    remove_operation_class: ClassVar[type[HareOperation]] = RemoveFunction
    rename_operation_class: ClassVar[type[HareOperation] | None] = RenameFunction


class SequenceDiff(DeclaredSchemaObjectDiff):
    """The sequence operations of a model."""

    object_type: ClassVar[type[SchemaObjectType]] = SequenceObjectType
    add_operation_class: ClassVar[type[HareOperation]] = AddSequence
    alter_operation_class: ClassVar[type[HareOperation] | None] = AlterSequence
    remove_operation_class: ClassVar[type[HareOperation]] = RemoveSequence
    rename_operation_class: ClassVar[type[HareOperation] | None] = RenameSequence


class PolicyDiff(DeclaredSchemaObjectDiff):
    """The row level security policy operations of a model."""

    object_type: ClassVar[type[SchemaObjectType]] = PolicyObjectType
    add_operation_class: ClassVar[type[HareOperation]] = AddPolicy
    alter_operation_class: ClassVar[type[HareOperation] | None] = AlterPolicy
    remove_operation_class: ClassVar[type[HareOperation]] = RemovePolicy
    rename_operation_class: ClassVar[type[HareOperation] | None] = RenamePolicy
