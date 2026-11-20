from __future__ import annotations

from typing import TYPE_CHECKING

from hare.migrations.operations.dependencies.operation_dependency import OperationDependency
from hare.migrations.operations.enums import DependencyType
from hare.migrations.operations.hare_operation import HareOperation
from hare.migrations.state.state import State

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class DropSchema(HareOperation):
    """Drop a database schema."""

    def __init__(self, schema_name: str) -> None:
        self.schema_name = schema_name

    def describe(self) -> str:
        return f"Drop schema {self.schema_name}"

    def get_provided_dependencies(self, app_label: str, state: State) -> list[OperationDependency]:
        return [OperationDependency.of_schema(self.schema_name, DependencyType.REMOVED)]

    def get_required_dependencies(self, app_label: str, state: State) -> list[OperationDependency]:
        return [OperationDependency.of_schema(self.schema_name, DependencyType.CREATED)]

    def state_forward(self, app_label: str, state: State) -> None:
        state.schemas.discard((state.default_connections.get(app_label), self.schema_name))
        state.record_native_operation(app_label, self)

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._toggle_schema(self.schema_name, False, state_editor)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._toggle_schema(self.schema_name, True, state_editor)
