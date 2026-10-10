from __future__ import annotations

from collections.abc import Hashable
from typing import TYPE_CHECKING, Any

from hare.ddl.indexes.index import Index
from hare.migrations.autodetection.diffs.schema_object_diff import SchemaObjectDiff
from hare.migrations.autodetection.state_signatures import StateSignatures
from hare.migrations.operations import AddIndex, HareOperation, RemoveIndex, RenameIndex
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.state.model_state import ModelState


class IndexDiff(SchemaObjectDiff):
    """The index operations of a model. A ``Meta.indexes`` entry given as a tuple of field names
    stands for a plain unnamed index; only an ``Index`` declared with a name is renamed. Two
    indexes of the same shape are the same index whatever their names, unless both can be renamed.

    Args:
        old_state: The model's state before.
        new_state: The model's state after.
        index_flag_altered_field_names: Fields whose own ``db_index`` flips - their ``AlterField``
            creates or drops the index, so it is left out here.
    """

    def __init__(self, old_state: ModelState, new_state: ModelState, index_flag_altered_field_names: set[str]) -> None:
        super().__init__(old_state, new_state)
        self.implicit_indexes = [Index(fields=(field_name,)) for field_name in index_flag_altered_field_names]
        #: The ids of the indexes declared as ``Index``, not as a tuple of field names.
        self.declared_index_ids: set[int] = set()

    def get_objects(self, model_state: ModelState) -> list[Any]:
        indexes: list[Index] = []
        for entry in model_state.get_option_list(ModelOption.INDEXES):
            index = entry if isinstance(entry, Index) else Index(fields=tuple(entry))
            if index in self.implicit_indexes:
                continue
            if isinstance(entry, Index):
                self.declared_index_ids.add(id(index))
            indexes.append(index)
        return indexes

    def get_signature(self, schema_object: Any) -> Hashable:
        return StateSignatures.get_index_signature(schema_object)

    def can_be_renamed(self, schema_object: Any) -> bool:
        return id(schema_object) in self.declared_index_ids and bool(schema_object.name)

    def is_unchanged(self, old_object: Any, new_object: Any) -> bool:
        if self.get_signature(old_object) != self.get_signature(new_object):
            return False
        renames = self.can_be_renamed(old_object) and self.can_be_renamed(new_object)
        return not renames or old_object.name == new_object.name

    def get_add_operation(self, schema_object: Any) -> HareOperation:
        return AddIndex(model_name=self.new_state.name, index=schema_object)

    def get_remove_operation(self, schema_object: Any) -> HareOperation:
        index_name = schema_object.name
        is_plain_field_index = (
            type(schema_object) is Index and not schema_object.get_name_parts() and not schema_object.unique
        )
        if not index_name and (not schema_object.fields or not is_plain_field_index):
            # Such an index is identified by the name it was created under, on the table the model
            # has when this RemoveIndex runs.
            index_name = schema_object.get_generated_expression_name(
                self.new_state.table or self.new_state.name.lower()
            )
        return RemoveIndex(
            model_name=self.new_state.name,
            name=index_name,
            fields=list(schema_object.field_names) if not index_name and schema_object.fields else None,
        )

    def get_rename_operation(self, old_object: Any, new_object: Any) -> HareOperation:
        return RenameIndex(model_name=self.new_state.name, old_name=old_object.name, new_name=new_object.name)
