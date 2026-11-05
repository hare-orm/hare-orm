from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any

from hare.migrations.operations.hare_operation import HareOperation
from hare.migrations.operations.operation import Operation

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.state.state import State


class SeparateDatabaseAndState(HareOperation):
    """Changes the migration state and the database separately - ``state_operations`` change only the
    models the migrations know, ``database_operations`` only the database. For a change the
    database already has, or one made in two steps::

        # The field leaves the models first; its column is dropped by a later migration.
        SeparateDatabaseAndState(state_operations=[RemoveField(model_name="Book", name="legacy_code")])

    Unapplied, the database operations run backwards in reverse order and the state goes back. A
    migration squash doesn't merge any operation across it.

    Args:
        database_operations: The operations run on the database - their changes to the state only
            tell each next one what the database holds.
        state_operations: The operations applied to the migration state.
    """

    def __init__(
        self, database_operations: list[Operation] | None = None, state_operations: list[Operation] | None = None
    ) -> None:
        self.database_operations = list(database_operations or [])
        self.state_operations = list(state_operations or [])
        self.reversible = all(operation.reversible for operation in self.database_operations)
        self.reduces_to_sql = all(operation.reduces_to_sql for operation in self.database_operations)

    def snapshot(self) -> SeparateDatabaseAndState:
        """Copy both branches through each child's declaration snapshot protocol."""
        result = copy(self)
        result.state_operations = [operation.snapshot() for operation in self.state_operations]
        result.database_operations = [operation.snapshot() for operation in self.database_operations]
        return result

    def describe(self) -> str:
        return "Custom state/database change combination"

    def get_table_model_names(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                model_name
                for operation in self.database_operations
                for model_name in operation.get_table_model_names()
            )
        )

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        if not self.database_operations:
            kwargs.pop("database_operations", None)
        if not self.state_operations:
            kwargs.pop("state_operations", None)
        return path, args, kwargs

    def state_forward(self, app_label: str, state: State) -> None:
        for operation in self.state_operations:
            operation.state_forward(app_label, state)

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        from hare.migrations.migration import Migration

        migration = Migration("separate_database", app_label, operations=self.database_operations)
        migration.atomic = False
        await migration.apply(old_state, schema_editor=state_editor)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        from hare.migrations.migration import Migration

        migration = Migration("separate_database", app_label, operations=self.database_operations)
        migration.atomic = False
        await migration.unapply(new_state, schema_editor=state_editor)
