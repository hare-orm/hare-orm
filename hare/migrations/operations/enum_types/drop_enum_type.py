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


class DropEnumType(EnumTypeOperation):
    """Drops a database ``ENUM`` type no column is of any more; going back creates it again. Skipped
    on a database without ``ENUM`` types.

    Args:
        name: The type's name.
        labels: Its labels, in order - the type going back creates.
        schema: The schema the type lives in - None for the connection's default one.
    """

    def __init__(self, name: str, labels: Sequence[str], schema: str | None = None) -> None:
        DeclaredNames.raise_if_not_name(name, "DropEnumType.name")
        DeclaredNames.raise_if_not_name(schema, "DropEnumType.schema", optional=True)
        self.name = name
        self.schema = schema
        self.labels = self.get_checked_labels(labels, "DropEnumType.labels")

    def get_provided_dependencies(self, app_label: str) -> list[OperationDependency]:
        return [OperationDependency.of_enum_type(self.schema, self.name, DependencyType.REMOVED)]

    def get_required_dependencies(self, app_label: str, state: State) -> list[OperationDependency]:
        """Every field of the type - removed or changed first."""
        return [
            OperationDependency.of_field(*model_key, field_name, DependencyType.CHANGED)
            for model_key, model_state in state.models.items()
            for field_name, field in model_state.fields.items()
            if (enum_type := getattr(field, "requires_enum_type", None)) is not None
            and enum_type.key == (self.schema, self.name)
        ]

    def describe(self) -> str:
        return f"Drop ENUM type {self.get_type_label()}"

    def state_forward(self, app_label: str, state: State) -> None:
        state.enum_types.pop(EnumType(self.name, self.labels, self.schema).key, None)

    def get_referenced_model_labels(self, app_label: str) -> frozenset[str] | None:
        return frozenset()

    async def database_forward(
        self, app_label: str, old_state: State, new_state: State, state_editor: BaseSchemaEditor | None = None
    ) -> None:
        if self._runs_enum_types(state_editor, f"dropping the ENUM type {self.name}"):
            await state_editor.enum_types.drop_enum_type(EnumType(self.name, self.labels, self.schema))  # type: ignore[union-attr]

    async def database_backward(
        self, app_label: str, old_state: State, new_state: State, state_editor: BaseSchemaEditor | None = None
    ) -> None:
        if self._runs_enum_types(state_editor, f"creating the ENUM type {self.name}"):
            await state_editor.enum_types.create_enum_type(EnumType(self.name, self.labels, self.schema))  # type: ignore[union-attr]
