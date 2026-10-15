from __future__ import annotations

from typing import Any

from hare.ddl.indexes.index import Index
from hare.migrations.operations.indexes.remove_index import RemoveIndex


class RemoveFieldIndex(RemoveIndex):
    """Removes the index a field's ``db_index=True`` gives a table created with the field.

    A squash puts it after a ``CreateModel`` it folded an ``AddField`` into: an added field has no
    index until its ``AddIndex``, a field of a created model has one at once. The ``AddIndex``
    that follows cancels out with it. Written to a migration as a ``RemoveIndex``.

    Args:
        model_name: The model.
        field_name: The field.
    """

    def __init__(self, model_name: str, field_name: str) -> None:
        super().__init__(model_name, fields=[field_name])
        self.field_name = field_name

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        return RemoveIndex(self.model_name, fields=[self.field_name]).deconstruct()

    def get_known_removed_object(self) -> Index:
        return Index(fields=(self.field_name,))
