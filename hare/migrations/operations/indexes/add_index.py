from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.schema_objects.add_schema_object import AddSchemaObject
from hare.migrations.operations.schema_objects.object_types.index_object_type import IndexObjectType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.indexes.index import Index
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class AddIndex(AddSchemaObject):
    """Adds an index to a model.

    Args:
        model_name: The model.
        index: The index.
        concurrently: Build it without blocking writes (Postgres ``CREATE INDEX CONCURRENTLY``) -
            the migration needs ``atomic = False``.
    """

    object_type: ClassVar[type[IndexObjectType]] = IndexObjectType

    def __init__(self, model_name: str, index: Index, *, concurrently: bool = False) -> None:
        super().__init__(model_name)
        self.index = index
        self.concurrently = concurrently

    @property
    def schema_object(self) -> Any:
        return self.index

    def get_type_options(self) -> dict[str, Any]:
        return {"concurrently": self.concurrently}

    def get_description_action(self) -> str:
        return "Concurrently add" if self.concurrently else "Add"

    def check_can_run(self, state_editor: BaseSchemaEditor) -> None:
        if self.concurrently:
            IndexObjectType.raise_if_concurrently_in_transaction(state_editor)
