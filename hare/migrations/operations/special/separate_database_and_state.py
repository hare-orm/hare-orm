from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.migrations.operations.hare_operation import HareOperation
from hare.migrations.operations.operation import Operation

if TYPE_CHECKING:  # pragma: nocoverage
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
        kwargs: dict[str, Any] = {}
        if self.database_operations:
            kwargs["database_operations"] = self.database_operations
        if self.state_operations:
            kwargs["state_operations"] = self.state_operations
        return f"hare.migrations.operations.{type(self).__name__}", [], kwargs

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
        from_state = old_state
        for operation in self.database_operations:
            to_state = from_state.clone()
            operation.state_forward(app_label, to_state)
            if not operation.touches_swapped_model(app_label, from_state, to_state):
                await operation.database_forward(app_label, from_state, to_state, state_editor)
            from_state = to_state

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        # The states the database operations went through forwards, from the state before this
        # operation (``new_state`` when going backwards).
        states = [new_state]
        for operation in self.database_operations:
            next_state = states[-1].clone()
            operation.state_forward(app_label, next_state)
            states.append(next_state)
        for index in range(len(self.database_operations) - 1, -1, -1):
            operation = self.database_operations[index]
            before_state, after_state = states[index], states[index + 1]
            if not operation.touches_swapped_model(app_label, before_state, after_state):
                await operation.database_backward(app_label, after_state, before_state, state_editor)
