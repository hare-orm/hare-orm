from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING

from hare.exceptions import ConfigurationError
from hare.migrations.operations.dependencies.operation_dependency import OperationDependency
from hare.migrations.operations.enums import DependencyType
from hare.migrations.operations.hare_operation import HareOperation

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.operations.operation import Operation
    from hare.migrations.reports.operation_effect import OperationEffect
    from hare.migrations.state.state import State


class AppOperation(HareOperation):
    """An existing operation on another app's models in the same database transaction."""

    def __init__(self, app_label: str, operation: Operation) -> None:
        self.app_label = app_label
        self.operation = operation
        self.atomic = operation.atomic
        self.reversible = operation.reversible
        self.reduces_to_sql = operation.reduces_to_sql

    @staticmethod
    def unwrap(app_label: str, operation: Operation) -> tuple[str, Operation]:
        """Return the owning app and the operation it executes."""
        while isinstance(operation, AppOperation):
            app_label, operation = operation.app_label, operation.operation
        return app_label, operation

    def snapshot(self) -> AppOperation:
        result = copy(self)
        result.operation = self.operation.snapshot()
        return result

    def describe(self) -> str:
        return f"{self.operation.describe()} ({self.app_label})"

    def get_nested_operations(self) -> tuple[Operation, ...]:
        return (self.operation,)

    def get_required_dependencies(self, app_label: str, state: State) -> list[OperationDependency]:
        return [
            *self.operation.get_required_dependencies(self.app_label, state),
            *self.operation.generation_dependencies,
            *(
                OperationDependency.of_model(self.app_label, name, DependencyType.CREATED)
                for name in self.operation.get_table_model_names()
                if (self.app_label, name) in state.models
            ),
        ]

    def get_provided_dependencies(self, app_label: str, state: State) -> list[OperationDependency]:
        return self.operation.get_provided_dependencies(self.app_label, state)

    def state_forward(self, app_label: str, state: State) -> None:
        if state.default_connections.get(self.app_label) != state.default_connections.get(app_label):
            raise ConfigurationError(
                f"An operation of {self.app_label!r} cannot execute in a migration "
                f"of {app_label!r} on another database."
            )
        self.operation.state_forward(self.app_label, state)

    def touches_swapped_model(self, app_label: str, *states: State) -> bool:
        return self.operation.touches_swapped_model(self.app_label, *states)

    def runs_in_tenant_schema(self, app_label: str, *states: State) -> bool:
        return self.operation.runs_in_tenant_schema(self.app_label, *states)

    def raise_if_unsupported(
        self, app_label: str, old_state: State, new_state: State, schema_editor: BaseSchemaEditor
    ) -> None:
        self.operation.raise_if_unsupported(self.app_label, old_state, new_state, schema_editor)

    def get_effect(
        self, app_label: str, old_state: State, new_state: State, schema_editor: BaseSchemaEditor
    ) -> OperationEffect:
        return self.operation.get_effect(self.app_label, old_state, new_state, schema_editor)

    async def database_forward(
        self, app_label: str, old_state: State, new_state: State, state_editor: BaseSchemaEditor | None = None
    ) -> None:
        await self.operation.database_forward(self.app_label, old_state, new_state, state_editor)

    async def database_backward(
        self, app_label: str, old_state: State, new_state: State, state_editor: BaseSchemaEditor | None = None
    ) -> None:
        await self.operation.database_backward(self.app_label, old_state, new_state, state_editor)
