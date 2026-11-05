from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.migrations.operations.operation import Operation
    from hare.migrations.state.prepared_operation_states import PreparedOperationStates
    from hare.migrations.state.state import State


@dataclass(frozen=True, slots=True)
class PreparedOperation:
    """An operation and independent snapshots of both sides of its change.

    Separate database/state declarations retain both child sequences. The state
    sequence determines ``after``; database children start from ``before`` and
    never replace the migration's declared state.
    """

    operation: Operation
    before: State
    after: State
    state_operations: PreparedOperationStates | None = None
    database_operations: PreparedOperationStates | None = None
