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


class CreateEnumType(EnumTypeOperation):
    """Creates a database ``ENUM`` type before the columns of it (``NativeEnumField``) - the state
    records it. Skipped on a database without ``ENUM`` types.

    Args:
        name: The type's name.
        labels: Its labels, in order.
        schema: The schema the type lives in - None for the connection's default one.
    """

    def __init__(self, name: str, labels: Sequence[str], schema: str | None = None) -> None:
        DeclaredNames.raise_if_not_name(name, "CreateEnumType.name")
        DeclaredNames.raise_if_not_name(schema, "CreateEnumType.schema", optional=True)
        self.name = name
        self.schema = schema
        self.labels = self.get_checked_labels(labels, "CreateEnumType.labels")

    def get_provided_dependencies(self, app_label: str) -> list[OperationDependency]:
        return [OperationDependency.of_enum_type(self.schema, self.name, DependencyType.CREATED)]

    def describe(self) -> str:
        return f"Create ENUM type {self.get_type_label()}"

    def state_forward(self, app_label: str, state: State) -> None:
        enum_type = EnumType(self.name, self.labels, self.schema)
        state.enum_types[enum_type.key] = enum_type

    def get_referenced_model_labels(self, app_label: str) -> frozenset[str] | None:
        return frozenset()

    async def database_forward(
        self, app_label: str, old_state: State, new_state: State, state_editor: BaseSchemaEditor | None = None
    ) -> None:
        if self._runs_enum_types(state_editor, f"creating the ENUM type {self.name}"):
            await state_editor.enum_types.create_enum_type(EnumType(self.name, self.labels, self.schema))  # type: ignore[union-attr]

    async def database_backward(
        self, app_label: str, old_state: State, new_state: State, state_editor: BaseSchemaEditor | None = None
    ) -> None:
        if self._runs_enum_types(state_editor, f"dropping the ENUM type {self.name}"):
            await state_editor.enum_types.drop_enum_type(EnumType(self.name, self.labels, self.schema))  # type: ignore[union-attr]
