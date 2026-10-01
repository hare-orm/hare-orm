from __future__ import annotations

from typing import TYPE_CHECKING

from hare.core.log import logger
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.base.operation import Operation
from hare.migrations.state.project.model_state import ModelState
from hare.migrations.state.project.state import State

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.editor import BaseSchemaEditor


class HareOperation(Operation):
    def state_forward(self, app_label: str, state: State) -> None:
        return None

    @staticmethod
    def get_model_state(state: State, app_label: str, model_name: str) -> ModelState:
        model = state.models.get((app_label, model_name))
        if not model:
            raise IncompatibleStateError()

        return model

    @staticmethod
    async def _toggle_schema(schema_name: str, create: bool, state_editor: BaseSchemaEditor | None) -> None:
        if not state_editor:
            return
        if not state_editor.client.dialect.supports_schemas:
            # No schemas on this dialect - nothing to create or drop.
            return
        if create:
            await state_editor.create_schema(schema_name)
        else:
            await state_editor.drop_schema(schema_name)

    @staticmethod
    async def _toggle_extension(extension_name: str, create: bool, state_editor: BaseSchemaEditor | None) -> None:
        if not state_editor:
            return
        if not state_editor.client.dialect.supports_extensions:
            logger.warning(
                "Skipping %s EXTENSION %r on %s - it has no extensions.",
                "CREATE" if create else "DROP",
                extension_name,
                state_editor.client.dialect,
            )
            return
        if create:
            await state_editor.create_extension(extension_name)
        else:
            await state_editor.drop_extension(extension_name)

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        return None

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        return None

    async def run(
        self,
        app_label: str,
        state: State,
        dry_run: bool,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        old_state = state.clone() if (not dry_run and state_editor) else None
        self.state_forward(app_label, state)
        if dry_run or not state_editor or self.touches_swapped_model(app_label, state):
            return
        await self.database_forward(app_label, old_state, state, state_editor)  # type: ignore[arg-type]
