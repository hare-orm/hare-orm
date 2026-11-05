from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from hare.exceptions import ConfigurationError
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.dependencies.prepared_operation_graph import PreparedOperationGraph
from hare.migrations.state.prepared_operation_states import PreparedOperationStates
from hare.migrations.state.state_values import StateValues

if TYPE_CHECKING:
    from hare.migrations.operations.operation import Operation
    from hare.migrations.state.state import State


class MigrationOptimizer:
    """Shorten an ordered migration sequence without changing its full resulting state.

    Rewrite proposals and permission to move operations are separate contracts.
    Dependencies are evaluated against prepared states, including the real state
    before a non-initial squash. Raw SQL, data operations and custom operations
    without explicit scope permission remain ordering barriers.
    """

    def __init__(self, app_label: str) -> None:
        self.app_label = app_label

    def optimize(self, operations: Sequence[Operation], state_before: State) -> list[Operation]:
        """Prepare snapshots and accept shortening rewrites until no proposal is valid.

        Args:
            operations: Valid operations in execution order; declarations stay unchanged.
            state_before: The actual schema before the first operation, preserved unchanged.

        Returns:
            Independent operation declarations with the same final state and data-operation order.
        """
        current = operations
        while True:
            graph = PreparedOperationGraph(self.app_label, current, state_before)
            optimized = self.optimize_once(graph)
            if optimized is None:
                return [item.operation for item in graph.states.operations]
            current = optimized

    def optimize_once(self, graph: PreparedOperationGraph) -> list[Operation] | None:
        """Accept the first legal pair replacement, checking its complete affected state segment."""
        operations = [item.operation for item in graph.states.operations]
        for index, operation in enumerate(operations):
            moves_right = True
            for later_index in range(index + 1, len(operations)):
                rewrite = operation.get_rewrite(operations[later_index], self.app_label)
                if rewrite is not None and len(rewrite.operations) < 2:
                    between = operations[index + 1 : later_index]
                    candidates = []
                    if moves_right:
                        candidates.append([*between, *rewrite.operations])
                    if not moves_right and all(
                        graph.can_move_across(position, later_index) for position in range(index + 1, later_index)
                    ):
                        candidates.append([*rewrite.operations, *between])
                    for candidate in candidates:
                        if self.preserves_state(graph, index, later_index, candidate):
                            return [*operations[:index], *candidate, *operations[later_index + 1 :]]
                moves_right = moves_right and graph.can_move_across(index, later_index)
        return None

    def preserves_state(
        self, graph: PreparedOperationGraph, first: int, last: int, replacement: Sequence[Operation]
    ) -> bool:
        """Compare every state declaration without generating a second autodetection diff."""
        before = graph.states.get(first)
        expected = graph.states.get(last + 1)
        try:
            actual = PreparedOperationStates(self.app_label, replacement, before).after
        except (ConfigurationError, IncompatibleStateError):
            return False
        return StateValues.get(actual) == StateValues.get(expected)
