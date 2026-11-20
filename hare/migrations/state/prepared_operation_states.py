from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from hare.migrations.state.prepared_operation import PreparedOperation
from hare.migrations.state.state_snapshot import StateSnapshot

if TYPE_CHECKING:
    from hare.migrations.operations.operation import Operation
    from hare.migrations.state.state import State


class PreparedOperationStates:
    """Prepare private boundaries once, preserving arbitrary nested state changes.

    Execution, inspection and optimization use the same operation tree. State
    callbacks run once during preparation. Working states and renderings are
    materialized by consumers without mutating retained declarations.
    """

    def __init__(self, app_label: str, operations: Sequence[Operation], state_before: State) -> None:
        self._prepare(app_label, tuple(operation.snapshot() for operation in operations), StateSnapshot(state_before))

    @classmethod
    def _from_snapshots(
        cls, app_label: str, operations: Sequence[Operation], before_snapshot: StateSnapshot
    ) -> PreparedOperationStates:
        prepared = cls.__new__(cls)
        prepared._prepare(app_label, operations, before_snapshot)
        return prepared

    def _prepare(self, app_label: str, operations: Sequence[Operation], before_snapshot: StateSnapshot) -> None:
        from hare.migrations.operations.special.separate_database_and_state import SeparateDatabaseAndState

        self.app_label = app_label
        self.before_snapshot = before_snapshot
        current = self.before_snapshot
        prepared = []
        for operation in operations:
            before = current
            database_operations = None
            if isinstance(operation, SeparateDatabaseAndState):
                state_operations = type(self)._from_snapshots(app_label, operation.state_operations, before)
                database_operations = type(self)._from_snapshots(app_label, operation.database_operations, before)
                after = state_operations.after_snapshot
            else:
                working = before.materialize()
                operation.state_forward(app_label, working)
                after = StateSnapshot(working)
            prepared.append(PreparedOperation(operation, before, after, database_operations=database_operations))
            current = after
        self.operations: tuple[PreparedOperation, ...] = tuple(prepared)
        self.after_snapshot: StateSnapshot = current

    @property
    def before(self) -> State:
        return self.before_snapshot.materialize()

    @property
    def after(self) -> State:
        return self.after_snapshot.materialize()

    def get(self, position: int) -> State:
        """Return an independent state at a prepared boundary, without replaying callbacks."""
        return self.after if position == len(self.operations) else self.operations[position].before
