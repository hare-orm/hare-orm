from __future__ import annotations

from collections.abc import Mapping, Sequence
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
    actual before-state, including changes made by earlier operations. Provider keys
    are immutable facts of this prepared sequence; a rewrite creates a new graph
    instead of carrying those keys across changed operations.
    """

    def __init__(self, app_label: str, operations: Sequence[Operation], state_before: State) -> None:
        self.states = PreparedOperationStates(app_label, operations, state_before)
        self.provided_keys = tuple(
            frozenset(
                self.get_provided_key(dependency.for_database(item.before.default_connections.get(app_label)))
                for dependency in item.operation.get_provided_dependencies(app_label, item.after)
            )
            for item in self.states.operations
        )
        self.required = tuple(
            (
                *(
                    dependency.for_database(item.before.default_connections.get(app_label))
                    for dependency in (
                        *item.operation.get_required_dependencies(app_label, item.before),
                        *item.operation.generation_dependencies,
                    )
                ),
            )
            for item in self.states.operations
        )

    def can_move_across(self, first: int, second: int) -> bool:
        """Require both scope permission and the absence of dependencies in either direction."""
        app_label = self.states.app_label
        first_operation = self.states.operations[first].operation
        second_operation = self.states.operations[second].operation
        if not (
            first_operation.can_move_across(second_operation, app_label)
            and second_operation.can_move_across(first_operation, app_label)
        ):
            return False
        # A preceding removal can erase a later operation's requirement from its
        # immediate state. Inspect the earlier real boundary as well before swapping.
        earlier, later = sorted((first, second))
        before = self.states.get(earlier)
        try:
            later_required = (
                *self.required[later],
                *(
                    dependency.for_database(before.default_connections.get(app_label))
                    for dependency in self.states.operations[later].operation.get_required_dependencies(
                        app_label, before
                    )
                ),
            )
        except (ConfigurationError, IncompatibleStateError):
            return False
        return not self.depends_on(self.required[earlier], later) and not self.depends_on(later_required, earlier)

    def depends_on(self, requirements: Sequence[OperationDependency], provider: int) -> bool:
        """Match creation, change and removal requirements using the sorter's dependency rules."""
        provided = self.provided_keys[provider]
        return any(not provided.isdisjoint(self.get_provider_keys(item)) for item in requirements)

    @staticmethod
    def get_provider_keys(requirement: OperationDependency) -> list[tuple[Any, ...]]:
        """The provided dependencies that meet a requirement: a creation by the creation, a removal by the
        removal - of the model too, for its members - and a change by anything done to it.

        Args:
            requirement: The requirement.

        Returns:
            The keys of the provided dependencies, ``(subject, app, model, name, database_alias, type)`` each.
        """
        key = (
            requirement.subject,
            requirement.app_label,
            requirement.model_name,
            requirement.name,
            requirement.database_alias,
        )
        model_removed = (DependencySubject.MODEL, requirement.app_label, requirement.model_name, None, None)
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
            ``(subject, app, model, name, database_alias, type)``.
        """
        return (
            dependency.subject,
            dependency.app_label,
            dependency.model_name,
            dependency.name,
            dependency.database_alias,
            dependency.dependency_type,
        )

    @classmethod
    def get_provider_positions(
        cls,
        requirement: OperationDependency,
        providers: Mapping[tuple[Any, ...], Sequence[int]],
        position: int,
    ) -> set[int]:
        """Eligible providers of a requirement, excluding the dependent operation itself."""
        return {
            provider_position
            for key in cls.get_provider_keys(requirement)
            for provider_position in providers.get(key, ())
            if provider_position != position
        }

    @classmethod
    def needs_waiting_provider(
        cls,
        requirement: OperationDependency,
        providers: Mapping[tuple[Any, ...], Sequence[int]],
        waiting: set[int],
        position: int,
    ) -> bool:
        """Whether a partial ordering still needs this requirement to be met.

        Providers are alternatives: one placed provider satisfies the requirement. No
        provider in this operation set means it needs no additional ordering edge.
        """
        positions = cls.get_provider_positions(requirement, providers, position)
        return bool(positions) and positions <= waiting

    @staticmethod
    def get_declaration_state(entries: Sequence[tuple[str, Operation]], state: State) -> State:
        """Expose created target declarations before sorting relations or splitting cycles."""
        from hare.migrations.operations.models.create_model import CreateModel

        declarations = state.clone()
        for app_label, operation in entries:
            if isinstance(operation, CreateModel):
                operation.state_forward(app_label, declarations)
        return declarations

    @classmethod
    def get_providers(
        cls,
        entries: Sequence[tuple[str, Operation]],
        connection_by_app: Mapping[str, str | None] | None = None,
        *,
        state: State,
    ) -> dict[tuple[Any, ...], list[int]]:
        """The positions of the operations providing each dependency.

        Args:
            entries: The operations, each with its app.
            connection_by_app: Native resource namespaces of the declaring apps.
            state: The migration state used to identify dependencies provided by operations.

        Returns:
            The key of each provided dependency -> the positions of the operations providing it.
        """
        state = cls.get_declaration_state(entries, state)
        providers: dict[tuple[Any, ...], list[int]] = {}
        for position, (app_label, operation) in enumerate(entries):
            for dependency in operation.get_provided_dependencies(app_label, state):
                scoped = dependency.for_database((connection_by_app or {}).get(app_label))
                providers.setdefault(cls.get_provided_key(scoped), []).append(position)
        return providers
