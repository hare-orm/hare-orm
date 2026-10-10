from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from hare.ddl.schema_objects.enum_type import EnumType
from hare.migrations.operations.hare_operation import HareOperation

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.state.state import State


class CreateEnumType(HareOperation):
    """Creates a database ``ENUM`` type before the columns of it (``NativeEnumField``). Not tracked in
    the state - the autodetector derives the types from the fields. Skipped on a database without
    ``ENUM`` types.

    Args:
        name: The type's name.
        labels: Its labels, in order.
    """

    def __init__(self, name: str, labels: Sequence[str]) -> None:
        self.name = name
        self.labels = tuple(labels)

    def describe(self) -> str:
        return f"Create ENUM type {self.name}"

    def get_referenced_model_labels(self, app_label: str) -> frozenset[str] | None:
        return frozenset()

    async def database_forward(
        self, app_label: str, old_state: State, new_state: State, state_editor: BaseSchemaEditor | None = None
    ) -> None:
        if self._runs_enum_types(state_editor, f"CREATE TYPE {self.name}"):
            await state_editor.enum_types.create_enum_type(EnumType(self.name, self.labels))  # type: ignore[union-attr]

    async def database_backward(
        self, app_label: str, old_state: State, new_state: State, state_editor: BaseSchemaEditor | None = None
    ) -> None:
        if self._runs_enum_types(state_editor, f"DROP TYPE {self.name}"):
            await state_editor.enum_types.drop_enum_type(EnumType(self.name, self.labels))  # type: ignore[union-attr]
