from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from hare.migrations.state.prepared_operation import PreparedOperation

if TYPE_CHECKING:
    from hare.migrations.operations.operation import Operation
    from hare.migrations.state.state import State


class PreparedOperationStates:
    """Prepare independent operation states without changing input declarations.

    Execution, reverse execution, SQL collection and safety inspection consume
    this same tree. No undo journal or declaration-origin heuristic participates
    in deciding which state an operation receives. Preparation performs no
    database work and does not invoke data-migration callbacks.
    """

    def __init__(self, app_label: str, operations: Sequence[Operation], state_before: State) -> None:
        from hare.migrations.operations.special.separate_database_and_state import SeparateDatabaseAndState

        self.app_label = app_label
        self.before: State = state_before.clone()
        current = self.before
        prepared = []
        for declaration in operations:
            operation = declaration.snapshot()
            before = current.clone()
            state_operations = database_operations = None
            if isinstance(operation, SeparateDatabaseAndState):
                state_operations = type(self)(app_label, operation.state_operations, before)
                database_operations = type(self)(app_label, operation.database_operations, before)
                after = state_operations.after.clone()
            else:
                after = before.clone()
                operation.state_forward(app_label, after)
                # A custom state operation may have changed a nested declaration
                # after reading rendered models. Publish a fresh rendering too.
                after = after.clone()
            prepared.append(PreparedOperation(operation, before, after, state_operations, database_operations))
            current = after
        self.operations: tuple[PreparedOperation, ...] = tuple(prepared)
        self.after: State = current.clone()

    def get(self, position: int) -> State:
        """Read the boundary before an operation, or after the complete sequence.

        Readers must not mutate a returned snapshot. Call ``clone()`` when a
        working state is required. All boundaries exist before execution starts.
        """
        return self.after if position == len(self.operations) else self.operations[position].before
