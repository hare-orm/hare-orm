from __future__ import annotations

from typing import ClassVar

from hare.migrations.autodetection.diffs.declared_schema_object_diff import DeclaredSchemaObjectDiff
from hare.migrations.operations import (
    AddDictionary,
    AddFunction,
    AddSequence,
    AlterDictionary,
    AlterFunction,
    AlterSequence,
    HareOperation,
    RemoveDictionary,
    RemoveFunction,
    RemoveSequence,
    RenameDictionary,
    RenameFunction,
    RenameSequence,
)
from hare.migrations.operations.schema_objects.object_types.dictionary_object_type import DictionaryObjectType
from hare.migrations.operations.schema_objects.object_types.function_object_type import FunctionObjectType
from hare.migrations.operations.schema_objects.object_types.schema_object_type import SchemaObjectType
from hare.migrations.operations.schema_objects.object_types.sequence_object_type import SequenceObjectType


class DictionaryDiff(DeclaredSchemaObjectDiff):
    """The dictionary operations of a model."""

    object_type: ClassVar[type[SchemaObjectType]] = DictionaryObjectType
    add_operation_class: ClassVar[type[HareOperation]] = AddDictionary
    alter_operation_class: ClassVar[type[HareOperation] | None] = AlterDictionary
    remove_operation_class: ClassVar[type[HareOperation]] = RemoveDictionary
    rename_operation_class: ClassVar[type[HareOperation] | None] = RenameDictionary


class FunctionDiff(DeclaredSchemaObjectDiff):
    """The function operations of a model."""

    removed_last: ClassVar[bool] = True

    object_type: ClassVar[type[SchemaObjectType]] = FunctionObjectType
    add_operation_class: ClassVar[type[HareOperation]] = AddFunction
    alter_operation_class: ClassVar[type[HareOperation] | None] = AlterFunction
    remove_operation_class: ClassVar[type[HareOperation]] = RemoveFunction
    rename_operation_class: ClassVar[type[HareOperation] | None] = RenameFunction


class SequenceDiff(DeclaredSchemaObjectDiff):
    """The sequence operations of a model."""

    removed_last: ClassVar[bool] = True

    object_type: ClassVar[type[SchemaObjectType]] = SequenceObjectType
    add_operation_class: ClassVar[type[HareOperation]] = AddSequence
    alter_operation_class: ClassVar[type[HareOperation] | None] = AlterSequence
    remove_operation_class: ClassVar[type[HareOperation]] = RemoveSequence
    rename_operation_class: ClassVar[type[HareOperation] | None] = RenameSequence
