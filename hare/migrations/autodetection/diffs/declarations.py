from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

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

if TYPE_CHECKING:
    from hare.migrations.state.model_state import ModelState


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

    @staticmethod
    def replaces_function(old_state: ModelState, new_state: ModelState) -> bool:
        """Whether an existing name needs a new physical function under that name."""
        old_functions = {function.name: function for function in FunctionObjectType.get_objects(old_state)}
        return any(
            not old.can_replace_in_place(function)
            for function in FunctionObjectType.get_objects(new_state)
            if (old := old_functions.get(function.name)) is not None
        )


class SequenceDiff(DeclaredSchemaObjectDiff):
    """The sequence operations of a model."""

    removed_last: ClassVar[bool] = True

    object_type: ClassVar[type[SchemaObjectType]] = SequenceObjectType
    add_operation_class: ClassVar[type[HareOperation]] = AddSequence
    alter_operation_class: ClassVar[type[HareOperation] | None] = AlterSequence
    remove_operation_class: ClassVar[type[HareOperation]] = RemoveSequence
    rename_operation_class: ClassVar[type[HareOperation] | None] = RenameSequence
