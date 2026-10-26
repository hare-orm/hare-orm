from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.migrations.operations import Operation
    from hare.migrations.state.state import State


class OperationStates:
    """The migration states between the operations of a migration, each made when a rule reads it - from the
    nearest one made before, so a migration whose rules read no state copies none.

    Args:
        app_label: The migration's app.
        operations: Its operations.
        state: The state before the first of them - left as it is.
    """

    def __init__(self, app_label: str, operations: Sequence[Operation], state: State) -> None:
        self.app_label = app_label
        self.operations = operations
        self.states_by_position: dict[int, State] = {0: state}

    def get(self, position: int) -> State:
        """The state before the operation at a position - after every operation at their count.

        Args:
            position: The operation's position.

        Returns:
            The state - not to be changed by the reader.
        """
        state = self.states_by_position.get(position)
        if state is not None:
            return state
        nearest_position = max(made_position for made_position in self.states_by_position if made_position < position)
        state = self.states_by_position[nearest_position].clone()
        for operation in self.operations[nearest_position:position]:
            operation.state_forward(self.app_label, state)
        self.states_by_position[position] = state
        return state
