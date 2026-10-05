from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.operations.operation import Operation


class MigrationOptimizer:
    """Shortens the operations of migrations squashed into one: each operation is folded into a
    later one it can be (``Operation.reduce()``) - a field added and removed cancels out, a model
    created and altered is created altered. An operation is moved across another only when the two
    touch no common model, so the result does what the operations did."""

    def __init__(self, app_label: str) -> None:
        """
        Args:
            app_label: The migrations' app.
        """
        self.app_label = app_label

    def optimize(self, operations: Sequence[Operation]) -> list[Operation]:
        """Folds the operations until no more fold.

        Args:
            operations: The operations, in the order they run.

        Returns:
            The shortened operations.
        """
        current_operations = list(operations)
        while True:
            optimized_operations = self.optimize_once(current_operations)
            if optimized_operations is None:
                return current_operations
            current_operations = optimized_operations

    def optimize_once(self, operations: list[Operation]) -> list[Operation] | None:
        """Folds the first pair of operations that folds.

        An operation folds with a later one when ``reduce()`` gives their replacement. The
        replacement takes the later operation's place when every operation in between may move
        across the earlier one, else the earlier one's place when every operation in between
        may move across the later one.

        Args:
            operations: The operations.

        Returns:
            The operations with one pair folded, None when no pair folds.
        """
        for index, operation in enumerate(operations):
            moves_right = True
            for later_index in range(index + 1, len(operations)):
                later_operation = operations[later_index]
                reduction = operation.reduce(later_operation, self.app_label)
                if isinstance(reduction, list):
                    in_between = operations[index + 1 : later_index]
                    if moves_right:
                        return [*operations[:index], *in_between, *reduction, *operations[later_index + 1 :]]
                    if all(
                        between_operation.reduce(later_operation, self.app_label) is True
                        for between_operation in in_between
                    ):
                        return [*operations[:index], *reduction, *in_between, *operations[later_index + 1 :]]
                    break
                if reduction is not True:
                    moves_right = False
        return None
