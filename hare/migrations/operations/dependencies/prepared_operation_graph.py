from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.exceptions import ConfigurationError
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.dependencies.constants import DEPENDENCY_MEMBER_SUBJECTS
from hare.migrations.operations.enums import DependencySubject, DependencyType
from hare.migrations.state.prepared_operation_states import PreparedOperationStates

if TYPE_CHECKING:
    from hare.migrations.operations.dependencies.operation_dependency import OperationDependency
    from hare.migrations.operations.operation import Operation
    from hare.migrations.state.state import State


class PreparedOperationGraph:
    """Operation scopes and dependencies evaluated against real prepared state boundaries.

    The sorter shares dependency matching with this graph. Optimization prepares
    an already ordered sequence; requirements for an operation are read from its
    actual before-state, including changes made by earlier operations.
    """

    def __init__(self, app_label: str, operations: Sequence[Operation], state_before: State) -> None:
        self.app_label = app_label
        self.states = PreparedOperationStates(app_label, operations, state_before)
        self.provided = tuple(
            tuple(item.operation.get_provided_dependencies(app_label)) for item in self.states.operations
        )
        self.required = tuple(
            (
                *item.operation.get_required_dependencies(app_label, item.before),
                *item.operation.generation_dependencies,
            )
            for item in self.states.operations
        )

    def can_move_across(self, first: int, second: int) -> bool:
        """Require both scope permission and the absence of dependencies in either direction."""
        first_operation = self.states.operations[first].operation
        second_operation = self.states.operations[second].operation
        if not (
            first_operation.can_move_across(second_operation, self.app_label)
            and second_operation.can_move_across(first_operation, self.app_label)
        ):
            return False
        # A preceding removal can erase a later operation's requirement from its
        # immediate state. Inspect the earlier real boundary as well before swapping.
        before = self.states.get(min(first, second))
        try:
            first_required = (
                *self.required[first],
                *first_operation.get_required_dependencies(self.app_label, before),
            )
            second_required = (
                *self.required[second],
                *second_operation.get_required_dependencies(self.app_label, before),
            )
        except (ConfigurationError, IncompatibleStateError):
            return False
        return not self.depends_on(first_required, second) and not self.depends_on(second_required, first)

    def depends_on(self, requirements: Sequence[OperationDependency], provider: int) -> bool:
        """Match creation, change and removal requirements using the sorter's dependency rules."""
        provided = {self.get_provided_key(item) for item in self.provided[provider]}
        return any(provided.intersection(self.get_provider_keys(item)) for item in requirements)

    @staticmethod
    def get_provider_keys(requirement: OperationDependency) -> list[tuple[Any, ...]]:
        """The provided dependencies that meet a requirement: a creation by the creation, a removal by the
        removal - of the model too, for its members - and a change by anything done to it.

        Args:
            requirement: The requirement.

        Returns:
            The keys of the provided dependencies, ``(subject, app, model, name, type)`` each.
        """
        key = (requirement.subject, requirement.app_label, requirement.model_name, requirement.name)
        model_removed = (DependencySubject.MODEL, requirement.app_label, requirement.model_name, None)
        if requirement.dependency_type is DependencyType.CREATED:
            return [(*key, DependencyType.CREATED)]
        provider_keys = [(*key, DependencyType.REMOVED)]
        if requirement.dependency_type is DependencyType.CHANGED:
            provider_keys += [(*key, DependencyType.CREATED), (*key, DependencyType.CHANGED)]
        if requirement.subject in DEPENDENCY_MEMBER_SUBJECTS:
            provider_keys.append((*model_removed, DependencyType.REMOVED))
        return provider_keys

    @staticmethod
    def get_provided_key(dependency: OperationDependency) -> tuple[Any, ...]:
        """The key a provided dependency is found by among those a requirement names
        (``get_provider_keys()``).

        Args:
            dependency: The provided dependency.

        Returns:
            ``(subject, app, model, name, type)``.
        """
        return (
            dependency.subject,
            dependency.app_label,
            dependency.model_name,
            dependency.name,
            dependency.dependency_type,
        )

    @classmethod
    def get_providers(cls, entries: Sequence[tuple[str, Operation]]) -> dict[tuple[Any, ...], list[int]]:
        """The positions of the operations providing each dependency.

        Args:
            entries: The operations, each with its app.

        Returns:
            The key of each provided dependency -> the positions of the operations providing it.
        """
        providers: dict[tuple[Any, ...], list[int]] = {}
        for position, (app_label, operation) in enumerate(entries):
            for dependency in operation.get_provided_dependencies(app_label):
                providers.setdefault(cls.get_provided_key(dependency), []).append(position)
        return providers
