from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hare.migrations.state.state_snapshot import StateSnapshot

if TYPE_CHECKING:
    from hare.migrations.operations.operation import Operation
    from hare.migrations.state.prepared_operation_states import PreparedOperationStates
    from hare.migrations.state.state import State


@dataclass(frozen=True, slots=True)
class PreparedOperation:
    """A detached operation and private declarations of both boundaries.

    A consumer materializes the states it needs. Rendered classes are owned by that
    consumer, never by every operation in the retained preparation tree.
    """

    operation: Operation
    before_snapshot: StateSnapshot
    after_snapshot: StateSnapshot
    database_operations: PreparedOperationStates | None = None

    @property
    def before(self) -> State:
        return self.before_snapshot.materialize()

    @property
    def after(self) -> State:
        return self.after_snapshot.materialize()
