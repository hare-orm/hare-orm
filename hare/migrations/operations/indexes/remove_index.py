from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.ddl.indexes.index import Index
from hare.exceptions import ConfigurationError
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.messages import REMOVE_INDEX_SELECTORS_CONFLICT, REMOVED_INDEX_NOT_FOUND
from hare.migrations.operations.schema_objects.object_types.index_object_type import IndexObjectType
from hare.migrations.operations.schema_objects.remove_schema_object import RemoveSchemaObject

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.state.state import State


class RemoveIndex(RemoveSchemaObject):
    """Removes an index selected by its name, fields or complete declaration.

    Args:
        model_name: The model.
        name: The index's name.
        fields: The fields of an unnamed index.
        index: An index declaration, selected structurally without binding a generated name.
            Exclusive with ``name`` and ``fields``.
        concurrently: Drop it without blocking reads and writes
            (``Features.supports_concurrent_indexes``) - the migration needs ``atomic = False``.
    """

    object_type: ClassVar[type[IndexObjectType]] = IndexObjectType

    def __init__(
        self,
        model_name: str,
        name: str | None = None,
        fields: list[str] | None = None,
        *,
        index: Index | None = None,
        concurrently: bool = False,
    ) -> None:
        if index is None:
            super().__init__(model_name, name, fields)
        else:
            if name or fields:
                raise ConfigurationError(REMOVE_INDEX_SELECTORS_CONFLICT)
            self.model_name = model_name
            self.name = None
            self.fields = None
        self.index = index
        self.concurrently = concurrently

    def find(self, state: State, app_label: str) -> Any:
        """Find the declared index before compiling its name for the current table."""
        if self.index is None:
            return super().find(state, app_label)
        model_state = self.get_model_state(state, app_label, self.model_name)
        for entry in self.object_type.get_objects(model_state):
            if self.object_type.is_same_object(entry, self.index):
                return entry
        raise IncompatibleStateError(REMOVED_INDEX_NOT_FOUND.format(index=self.index, model=model_state.name))

    def describe(self) -> str:
        if self.index is None:
            return super().describe()
        action = "Concurrently remove" if self.concurrently else "Remove"
        return f"{action} index {self.index!r} from {self.model_name}"

    def get_type_options(self) -> dict[str, Any]:
        return {"concurrently": self.concurrently}

    def check_can_run(self, state_editor: BaseSchemaEditor) -> None:
        if self.concurrently:
            IndexObjectType.raise_if_concurrently_in_transaction(state_editor)
