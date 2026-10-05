from __future__ import annotations

import heapq
from typing import TYPE_CHECKING, Any

from hare.exceptions import ConfigurationError
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.migrations.operations import AddField, AddIndex, CreateModel
from hare.migrations.operations.enums import DependencySubject, DependencyType

if TYPE_CHECKING:
    from hare.migrations.operations.dependencies.operation_dependency import OperationDependency
    from hare.migrations.operations.operation import Operation
    from hare.migrations.state.state import State


class OperationSorter:
    """Orders operations so that each runs after what it needs (``Operation.get_required_dependencies()``,
    met by the first operation providing it) - a stable order: an operation keeps its place among the
    others unless a dependency moves it after them. A cycle is broken by taking a relation out of a
    ``CreateModel`` into an ``AddField`` of its own.

    Args:
        state: The state before the operations.
    """

    #: The subjects a removed model takes along - removing the model removes them too.
    MEMBER_SUBJECTS = frozenset({DependencySubject.FIELD, DependencySubject.SCHEMA_OBJECT, DependencySubject.OPTIONS})

    def __init__(self, state: State) -> None:
        self.state = state

    def sort(self, entries: list[tuple[str, Operation]]) -> list[tuple[str, Operation]]:
        """The operations in the order they run.

        Args:
            entries: The operations, each with its app, in the order they were made.

        Returns:
            The ordered operations - a relation taken out of its CreateModel comes as an AddField.

        Raises:
            ConfigurationError: The operations need each other in a cycle no relation breaks.
        """
        entries = list(entries)
        while True:
            ordered, waiting = self.get_order(entries)
            if not waiting:
                return [entries[index] for index in ordered]
            entries = self.get_cycle_broken(entries, waiting)

    def get_required(self, app_label: str, operation: Operation) -> list[OperationDependency]:
        """What an operation needs - its own requirements and those it was made with.

        Args:
            app_label: The operation's app.
            operation: The operation.
        """
        return [
            *operation.get_required_dependencies(app_label, self.state),
            *operation.generation_dependencies,
        ]

    @classmethod
    def get_provider_keys(cls, requirement: OperationDependency) -> list[tuple[Any, ...]]:
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
        if requirement.subject in cls.MEMBER_SUBJECTS:
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
    def get_providers(cls, entries: list[tuple[str, Operation]]) -> dict[tuple[Any, ...], list[int]]:
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

    def get_order(self, entries: list[tuple[str, Operation]]) -> tuple[list[int], list[int]]:
        """The positions of the operations in the order they run, and those left waiting in a cycle.

        Args:
            entries: The operations, each with its app.

        Returns:
            The ordered positions, and the positions of the operations never ready.
        """
        providers = self.get_providers(entries)
        unmet_counts = [0] * len(entries)
        # The requirements a placed operation meets: (waiting position, requirement number).
        met_by: dict[int, list[tuple[int, int]]] = {}
        for position, (app_label, operation) in enumerate(entries):
            for requirement_number, requirement in enumerate(self.get_required(app_label, operation)):
                provider_positions = sorted(
                    {
                        provider_position
                        for key in self.get_provider_keys(requirement)
                        for provider_position in providers.get(key, ())
                        if provider_position != position
                    }
                )
                if not provider_positions:
                    continue
                unmet_counts[position] += 1
                for provider_position in provider_positions:
                    met_by.setdefault(provider_position, []).append((position, requirement_number))
        ready = [position for position, count in enumerate(unmet_counts) if count == 0]
        heapq.heapify(ready)
        met: set[tuple[int, int]] = set()
        ordered: list[int] = []
        while ready:
            position = heapq.heappop(ready)
            ordered.append(position)
            for waiting_position, requirement_number in met_by.get(position, ()):
                if (waiting_position, requirement_number) in met:
                    continue
                met.add((waiting_position, requirement_number))
                unmet_counts[waiting_position] -= 1
                if unmet_counts[waiting_position] == 0:
                    heapq.heappush(ready, waiting_position)
        placed = set(ordered)
        return ordered, [position for position in range(len(entries)) if position not in placed]

    def get_cycle_broken(
        self, entries: list[tuple[str, Operation]], waiting: list[int]
    ) -> list[tuple[str, Operation]]:
        """The operations with one relation of a waiting ``CreateModel`` that needs another waiting
        operation taken out into an ``AddField`` (with the indexes over it) right after it - a many-to-many
        relation rather than a foreign key.

        Args:
            entries: The operations, each with its app.
            waiting: The positions of the operations waiting in a cycle.

        Returns:
            The operations with the relation taken out.

        Raises:
            ConfigurationError: No waiting CreateModel has such a relation.
        """
        waiting_entries = [entries[position] for position in waiting]
        waiting_provided = {
            self.get_provided_key(dependency)
            for app_label, operation in waiting_entries
            for dependency in operation.get_provided_dependencies(app_label)
        }
        # A many-to-many relation first - it has no column, and its through model keeps its keys.
        for relation_types in ((ManyToManyFieldInstance,), (ForeignKeyFieldInstance,)):
            for position in waiting:
                app_label, operation = entries[position]
                if not isinstance(operation, CreateModel):
                    continue
                for field_name, field in operation.fields:
                    if not isinstance(field, relation_types):
                        continue
                    addition = AddField(model_name=operation.name, name=field_name, field=field)
                    if not any(
                        key in waiting_provided
                        for requirement in self.get_required(app_label, addition)
                        for key in self.get_provider_keys(requirement)
                    ):
                        continue
                    popped_field, indexes = operation.pop_field(field_name)
                    taken_out: list[tuple[str, Operation]] = [
                        (app_label, AddField(model_name=operation.name, name=field_name, field=popped_field)),
                        *((app_label, AddIndex(model_name=operation.name, index=index)) for index in indexes),
                    ]
                    return [*entries[: position + 1], *taken_out, *entries[position + 1 :]]
        described = "; ".join(operation.describe() for _app_label, operation in waiting_entries)
        raise ConfigurationError(f"The migration's operations need each other in a cycle: {described}")
