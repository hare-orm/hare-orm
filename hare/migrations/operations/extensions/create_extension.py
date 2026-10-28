from __future__ import annotations

from typing import TYPE_CHECKING

from hare.migrations.operations.dependencies.operation_dependency import OperationDependency
from hare.migrations.operations.enums import DependencyType
from hare.migrations.operations.hare_operation import HareOperation
from hare.migrations.state.state import State

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class CreateExtension(HareOperation):
    """Creates a database extension before the tables that need it - the
    state records it, so a model needing it again later creates it no second time.
    """

    backward_reads_state_left = False
    backward_reads_state_reached = False

    def __init__(self, extension_name: str) -> None:
        self.extension_name = extension_name

    def get_provided_dependencies(self, app_label: str) -> list[OperationDependency]:
        return [OperationDependency.of_extension(self.extension_name, DependencyType.CREATED)]

    def describe(self) -> str:
        return f"Create extension {self.extension_name}"

    def state_forward(self, app_label: str, state: State) -> None:
        state.extensions.add(self.extension_name)

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._toggle_extension(self.extension_name, True, state_editor)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        await self._toggle_extension(self.extension_name, False, state_editor)
