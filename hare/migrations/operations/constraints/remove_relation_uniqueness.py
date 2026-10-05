from __future__ import annotations

from typing import Any

from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.migrations.operations.constraints.declarations import RemoveConstraint


class RemoveRelationUniqueness(RemoveConstraint):
    """Removes the unique constraint over the key fields of a one-to-one relation to several
    fields, from a table created with the relation.

    A squash puts it after a ``CreateModel`` it folded the relation's ``AddField`` into: an added
    relation isn't unique until its ``AddConstraint``, a relation of a created model is at once.
    The ``AddConstraint`` that follows cancels out with it. Written to a migration as a
    ``RemoveConstraint``.

    Args:
        model_name: The model.
        key_field_names: The relation's key fields.
    """

    def __init__(self, model_name: str, key_field_names: tuple[str, ...]) -> None:
        super().__init__(model_name, fields=list(key_field_names))
        self.key_field_names = key_field_names

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        return RemoveConstraint(self.model_name, fields=list(self.key_field_names)).deconstruct()

    def get_known_removed_object(self) -> UniqueConstraint:
        return UniqueConstraint(fields=self.key_field_names)
