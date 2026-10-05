from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from hare.ddl.schema_objects.enum_type import EnumType
from hare.migrations.operations.hare_operation import HareOperation

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.state.state import State


class AlterEnumType(HareOperation):
    """Gives a database ``ENUM`` type other labels: new labels are added in place while the old ones
    keep their order; a removed or reordered label replaces the type, converting the columns of it -
    every value of them has to be among the new labels. Going back restores the old labels. Skipped
    on a database without ``ENUM`` types.

    Args:
        name: The type's name.
        old_labels: Its labels before.
        new_labels: Its labels after.
    """

    def __init__(self, name: str, old_labels: Sequence[str], new_labels: Sequence[str]) -> None:
        self.name = name
        self.old_labels = tuple(old_labels)
        self.new_labels = tuple(new_labels)

    def describe(self) -> str:
        return f"Alter ENUM type {self.name}"

    async def database_forward(
        self, app_label: str, old_state: State, new_state: State, state_editor: BaseSchemaEditor | None = None
    ) -> None:
        if self._runs_enum_types(state_editor, f"ALTER TYPE {self.name}"):
            await state_editor.enum_types.alter_enum_type(  # type: ignore[union-attr]
                EnumType(self.name, self.old_labels), EnumType(self.name, self.new_labels)
            )

    async def database_backward(
        self, app_label: str, old_state: State, new_state: State, state_editor: BaseSchemaEditor | None = None
    ) -> None:
        if self._runs_enum_types(state_editor, f"ALTER TYPE {self.name}"):
            await state_editor.enum_types.alter_enum_type(  # type: ignore[union-attr]
                EnumType(self.name, self.new_labels), EnumType(self.name, self.old_labels)
            )
