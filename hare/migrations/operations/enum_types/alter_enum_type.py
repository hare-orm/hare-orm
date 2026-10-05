from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from hare.ddl.declared_names import DeclaredNames
from hare.ddl.schema_objects.enum_type import EnumType
from hare.migrations.operations.dependencies.operation_dependency import OperationDependency
from hare.migrations.operations.enum_types.enum_type_operation import EnumTypeOperation
from hare.migrations.operations.enums import DependencyType

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.state.state import State


class AlterEnumType(EnumTypeOperation):
    """Gives a database ``ENUM`` type other labels: new labels are added in place while the old ones
    keep their order; a removed or reordered label replaces the type, converting the columns of it -
    every value of them has to be among the new labels. Going back restores the old labels. Skipped
    on a database without ``ENUM`` types.

    Args:
        name: The type's name.
        old_labels: Its labels before.
        new_labels: Its labels after.
        schema: The schema the type lives in - None for the connection's default one.
    """

    def __init__(
        self, name: str, old_labels: Sequence[str], new_labels: Sequence[str], schema: str | None = None
    ) -> None:
        DeclaredNames.raise_if_not_name(name, "AlterEnumType.name")
        DeclaredNames.raise_if_not_name(schema, "AlterEnumType.schema", optional=True)
        self.name = name
        self.schema = schema
        self.old_labels = self.get_checked_labels(old_labels, "AlterEnumType.old_labels")
        self.new_labels = self.get_checked_labels(new_labels, "AlterEnumType.new_labels")

    def get_provided_dependencies(self, app_label: str) -> list[OperationDependency]:
        return [OperationDependency.of_enum_type(self.schema, self.name, DependencyType.CHANGED)]

    def describe(self) -> str:
        return f"Alter ENUM type {self.get_type_label()}"

    def state_forward(self, app_label: str, state: State) -> None:
        enum_type = EnumType(self.name, self.new_labels, self.schema)
        state.enum_types[enum_type.key] = enum_type

    async def database_forward(
        self, app_label: str, old_state: State, new_state: State, state_editor: BaseSchemaEditor | None = None
    ) -> None:
        if self._runs_enum_types(state_editor, f"altering the ENUM type {self.name}"):
            await state_editor.enum_types.alter_enum_type(  # type: ignore[union-attr]
                EnumType(self.name, self.old_labels, self.schema), EnumType(self.name, self.new_labels, self.schema)
            )

    async def database_backward(
        self, app_label: str, old_state: State, new_state: State, state_editor: BaseSchemaEditor | None = None
    ) -> None:
        if self._runs_enum_types(state_editor, f"altering the ENUM type {self.name}"):
            await state_editor.enum_types.alter_enum_type(  # type: ignore[union-attr]
                EnumType(self.name, self.new_labels, self.schema), EnumType(self.name, self.old_labels, self.schema)
            )
