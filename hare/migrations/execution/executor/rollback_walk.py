from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from hare.migrations.constants import MISSING_MIGRATION_MESSAGE_TEMPLATE
from hare.migrations.exceptions import UnknownMigrationError
from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.migrations.state.state import State
from hare.migrations.state.state_change_journal import StateChangeJournal

if TYPE_CHECKING:
    from hare.migrations.execution.executor.migration_executor import MigrationExecutor
    from hare.migrations.execution.executor.plan_step import PlanStep
    from hare.migrations.state.declarations import OperationUndo


class RollbackWalk:
    """The state of a connection walked back through its migrations - one state, rendered once and taken back
    migration by migration with what the state change journal recorded of each, so a rollback of K migrations
    costs what applying them does.

    Args:
        keys: The migrations making the state, in plan order.
        state: The state after all of them.
        undos: What takes the state back from each operation, by migration.
    """

    def __init__(
        self, keys: Sequence[MigrationKey], state: State, undos: dict[MigrationKey, list[OperationUndo]]
    ) -> None:
        self.state = state
        self.undos = undos
        self.keys = list(keys)
        self.positions = {key: position for position, key in enumerate(self.keys)}
        #: How many of the migrations the state holds.
        self.position = len(self.keys)

    @staticmethod
    def build(executor: MigrationExecutor, applied: set[MigrationKey]) -> RollbackWalk:
        """Projects the migrations making the state of a connection forward, recording each operation's undo.

        Args:
            executor: The executor of the connection.
            applied: The migrations applied on the connection.

        Returns:
            The walk, at the state after all of them.

        Raises:
            UnknownMigrationError: A migration of the plan has no file.
            InconsistentMigrationStateError: A relation's target is missing after a migration.
        """
        state = State(default_connections=executor.get_default_connections())
        keys = executor.get_state_keys(applied)
        undos: dict[MigrationKey, list[OperationUndo]] = {}
        for key in keys:
            migration = executor.loader.graph.nodes[key]
            if migration is None:
                raise UnknownMigrationError(MISSING_MIGRATION_MESSAGE_TEMPLATE.format(key=key))
            undos[key] = [
                StateChangeJournal.forward(state, key.app_label, operation) for operation in migration.operations
            ]
            state.validate_relations_initialized()
        return RollbackWalk(keys, state, undos)

    def move_to_after(self, key: MigrationKey) -> list[OperationUndo]:
        """Takes the state back to right after a migration - the migrations after it still in the state are
        undone in the state alone - and gives what takes the migration itself back; the caller takes it back.

        Args:
            key: The migration - one before every migration taken back so far.

        Returns:
            What takes the state back from each of the migration's operations.
        """
        position = self.positions[key]
        while self.position > position + 1:
            self.position -= 1
            for undo in reversed(self.undos[self.keys[self.position]]):
                self.state.undo(undo)
        self.position = position
        return self.undos[key]

    @staticmethod
    def fits(plan: Sequence[PlanStep], state_keys: Sequence[MigrationKey]) -> bool:
        """Whether a plan only takes migrations back, each one before the ones taken back already - a
        rollback a walk goes through; a plan applying a migration, or taking back one after a migration
        taken back already, projects the state of each step anew.

        Args:
            plan: The steps.
            state_keys: The migrations making the state of the connection, in plan order.
        """
        positions = {key: position for position, key in enumerate(state_keys)}
        last_position = len(state_keys)
        for step in plan:
            position = positions.get(MigrationKey(app_label=step.migration.app_label, name=step.migration.name))
            if not step.backward or position is None or position >= last_position:
                return False
            last_position = position
        return True
