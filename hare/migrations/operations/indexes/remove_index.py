from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.operations.schema_objects.index_object_type import IndexObjectType
from hare.migrations.operations.schema_objects.remove_schema_object import RemoveSchemaObject

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.editor import BaseSchemaEditor


class RemoveIndex(RemoveSchemaObject):
    """Removes an index from a model, given by its name or its fields.

    Args:
        model_name: The model.
        name: The index's name.
        fields: The fields of an unnamed index.
        concurrently: Drop it without blocking reads and writes (Postgres ``DROP INDEX
            CONCURRENTLY``) - the migration needs ``atomic = False``.
    """

    object_type: ClassVar[type[IndexObjectType]] = IndexObjectType

    def __init__(
        self, model_name: str, name: str | None = None, fields: list[str] | None = None, *, concurrently: bool = False
    ) -> None:
        super().__init__(model_name, name, fields)
        self.concurrently = concurrently

    def get_type_options(self) -> dict[str, Any]:
        return {"concurrently": self.concurrently}

    def check_can_run(self, state_editor: BaseSchemaEditor) -> None:
        if self.concurrently:
            IndexObjectType.raise_if_concurrently_in_transaction(state_editor)
