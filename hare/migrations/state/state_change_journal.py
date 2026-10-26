from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.constants import OPERATIONS_MODULE_PREFIX
from hare.migrations.state.declarations import OperationUndo

if TYPE_CHECKING:
    from hare.migrations.operations.operation import Operation
    from hare.migrations.state.model_state import ModelState
    from hare.migrations.state.state import State


class StateChangeJournal:
    """What an operation changes in a state - the model states as they were before it, and its extensions,
    schemas and ``ENUM`` types - so the state is taken back operation by operation without a copy of the
    whole state per operation.

    Args:
        state: The state the operation changes.
    """

    #: The journal recording the operation running now - None outside of ``forward()``.
    recording: ClassVar[ContextVar[StateChangeJournal | None]] = ContextVar("state_change_journal", default=None)

    __slots__ = ("model_states_before", "recorded_owner_ids", "state")

    def __init__(self, state: State) -> None:
        self.state = state
        self.model_states_before: dict[tuple[str, str], ModelState | None] = {}
        self.recorded_owner_ids: set[int] = set()

    @staticmethod
    def forward(state: State, app_label: str, operation: Operation) -> OperationUndo:
        """Brings a state forward by an operation, recording what takes it back.

        Args:
            state: The state, changed in place.
            app_label: The migration's app.
            operation: The operation.

        Returns:
            What takes the state back to before the operation.
        """
        journal = StateChangeJournal(state)
        extensions, schemas, enum_types = frozenset(state.extensions), frozenset(state.schemas), dict(state.enum_types)
        if not StateChangeJournal.changes_state_through_containers(operation):
            # An operation of the application may change a field of a model state in place - every model
            # state is kept as it was.
            for key, model_state in state.models.items():
                journal.model_states_before[key] = model_state.clone()
        token = StateChangeJournal.recording.set(journal)
        try:
            operation.state_forward(app_label, state)
        finally:
            StateChangeJournal.recording.reset(token)
        return OperationUndo(journal.model_states_before, extensions, schemas, enum_types)

    @staticmethod
    def changes_state_through_containers(operation: Operation) -> bool:
        """Whether an operation changes a state only by setting the items and attributes of its models -
        the operations of hare, and a ``SeparateDatabaseAndState`` of them; never a field in place.

        Args:
            operation: The operation.
        """
        if not type(operation).state_forward.__module__.startswith(OPERATIONS_MODULE_PREFIX):
            return False
        return all(
            StateChangeJournal.changes_state_through_containers(state_operation)
            for state_operation in getattr(operation, "state_operations", ())
        )

    def record_change(self, owner: Any, key: Any) -> None:
        """Records a model state before its first change in the operation.

        Args:
            owner: The state (its models changed at ``key``) or a model state (changed itself).
            key: The key of the models changed - unused for a model state.
        """
        if owner is self.state:
            if key not in self.model_states_before:
                previous = self.state.models.get(key)
                self.model_states_before[key] = previous.clone() if previous is not None else None
            return
        if id(owner) in self.recorded_owner_ids:
            return
        self.recorded_owner_ids.add(id(owner))
        owner_key = (owner.app, owner.name)
        # A model state not in the state (one an operation builds) is recorded when it is put in.
        if owner_key not in self.model_states_before and self.state.models.get(owner_key) is owner:
            self.model_states_before[owner_key] = owner.clone()
