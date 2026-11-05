from __future__ import annotations

import heapq
from typing import TYPE_CHECKING

from hare.exceptions import ConfigurationError
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.migrations.operations import AddField, AddIndex, CreateModel, DeleteModel, RemoveField
from hare.migrations.operations.dependencies.operation_dependency import OperationDependency
from hare.migrations.operations.dependencies.prepared_operation_graph import PreparedOperationGraph
from hare.migrations.operations.enums import DependencySubject, DependencyType

if TYPE_CHECKING:
    from hare.migrations.operations.operation import Operation
    from hare.migrations.state.state import State


class OperationSorter:
    """Orders operations so that each runs after what it needs (``Operation.get_required_dependencies()``,
    met by the first operation providing it) - a stable order: an operation keeps its place among the
    others unless a dependency moves it after them. A cycle is broken by taking a relation out of a
    ``CreateModel`` into an ``AddField`` of its own, or out of a ``DeleteModel`` into a ``RemoveField`` before it.

    Args:
        state: The state before the operations.
    """

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
        entries = [(app_label, operation.snapshot()) for app_label, operation in entries]
        while True:
            ordered, waiting = self.get_order(entries)
            if not waiting:
                return [entries[index] for index in ordered]
            entries = self.get_cycle_broken(entries, waiting)

    def get_required(self, app_label: str, operation: Operation) -> list[OperationDependency]:
        """What an operation needs - its own requirements and those it was made with; a model of the
        running application no migration creates (``Meta.managed = False``) needs the models its relations
        point at, so an operation needing it needs them too.

        Args:
            app_label: The operation's app.
            operation: The operation.
        """
        required = [
            *operation.get_required_dependencies(app_label, self.state),
            *operation.generation_dependencies,
        ]
        return [*required, *self.get_unmanaged_targets_required(required)]

    def get_unmanaged_targets_required(self, required: list[OperationDependency]) -> list[OperationDependency]:
        """The creations of the models the relations of the unmanaged models required point at - through
        other unmanaged models too.

        Args:
            required: The requirements.

        Returns:
            The requirements of the targets' creation.
        """
        unmanaged_targets: list[OperationDependency] = []
        waiting = [
            (requirement.app_label, requirement.model_name)
            for requirement in required
            if requirement.subject is DependencySubject.MODEL
            and requirement.dependency_type is DependencyType.CREATED
            and requirement.model_name is not None
        ]
        seen: set[tuple[str, str]] = set()
        while waiting:
            model_key = waiting.pop()
            if model_key in seen:
                continue
            seen.add(model_key)
            model_state = next(
                (
                    model
                    for (app, name), model in self.state.environment.unmanaged_models.items()
                    if (app, name.lower()) == model_key
                ),
                None,
            )
            if model_state is None:
                continue
            references = [
                field.model_name
                for field in model_state.fields.values()
                if isinstance(field, (ForeignKeyFieldInstance, ManyToManyFieldInstance))
            ]
            references += [
                field.through_model
                for field in model_state.fields.values()
                if isinstance(field, ManyToManyFieldInstance) and field.through_model is not None
            ]
            for reference in references:
                target_app, target_name = self.state.get_model_key(reference)
                unmanaged_targets.append(OperationDependency.of_model(target_app, target_name, DependencyType.CREATED))
                waiting.append((target_app, target_name.lower()))
        return unmanaged_targets

    def get_order(self, entries: list[tuple[str, Operation]]) -> tuple[list[int], list[int]]:
        """The positions of the operations in the order they run, and those left waiting in a cycle.

        Args:
            entries: The operations, each with its app.

        Returns:
            The ordered positions, and the positions of the operations never ready.
        """
        providers = PreparedOperationGraph.get_providers(entries)
        unmet_counts = [0] * len(entries)
        # The requirements a placed operation meets: (waiting position, requirement number).
        met_by: dict[int, list[tuple[int, int]]] = {}
        for position, (app_label, operation) in enumerate(entries):
            for requirement_number, requirement in enumerate(self.get_required(app_label, operation)):
                provider_positions = sorted(
                    {
                        provider_position
                        for key in PreparedOperationGraph.get_provider_keys(requirement)
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
        relation rather than a foreign key; else one relation of a waiting ``DeleteModel`` another waiting
        operation needs removed taken out into a ``RemoveField`` right before it (a model deleted with the
        through model of its many-to-many relation).

        Args:
            entries: The operations, each with its app.
            waiting: The positions of the operations waiting in a cycle.

        Returns:
            The operations with the relation taken out.

        Raises:
            ConfigurationError: No waiting CreateModel or DeleteModel has such a relation.
        """
        waiting_entries = [entries[position] for position in waiting]
        waiting_provided = {
            PreparedOperationGraph.get_provided_key(dependency)
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
                        for key in PreparedOperationGraph.get_provider_keys(requirement)
                    ):
                        continue
                    creation, extracted_field, indexes = operation.split_field(field_name)
                    taken_out: list[tuple[str, Operation]] = [
                        (app_label, AddField(model_name=operation.name, name=field_name, field=extracted_field)),
                        *((app_label, AddIndex(model_name=operation.name, index=index)) for index in indexes),
                    ]
                    return [*entries[:position], (app_label, creation), *taken_out, *entries[position + 1 :]]
        if (removal := self.get_cycle_breaking_removal(entries, waiting)) is not None:
            position, removal_entry = removal
            return [*entries[:position], removal_entry, *entries[position:]]
        described = "; ".join(operation.describe() for _app_label, operation in waiting_entries)
        raise ConfigurationError(f"The migration's operations need each other in a cycle: {described}")

    def get_cycle_breaking_removal(
        self, entries: list[tuple[str, Operation]], waiting: list[int]
    ) -> tuple[int, tuple[str, Operation]] | None:
        """A relation of a waiting ``DeleteModel`` whose removal another waiting operation needs - its
        ``RemoveField`` and the position it goes to, right before the ``DeleteModel``.

        Args:
            entries: The operations, each with its app.
            waiting: The positions of the operations waiting in a cycle.

        Returns:
            The position and the removal - None for no such relation.
        """
        for relation_types in ((ManyToManyFieldInstance,), (ForeignKeyFieldInstance,)):
            for position in waiting:
                app_label, operation = entries[position]
                if not isinstance(operation, DeleteModel):
                    continue
                model_state = next(
                    (
                        model_state
                        for model_key, model_state in self.state.models.items()
                        if model_key[0] == app_label and model_key[1].lower() == operation.name.lower()
                    ),
                    None,
                )
                if model_state is None:
                    continue
                needed_keys = {
                    key
                    for other_position in waiting
                    if other_position != position
                    for requirement in self.get_required(*entries[other_position])
                    for key in PreparedOperationGraph.get_provider_keys(requirement)
                }
                for field_name, field in model_state.fields.items():
                    if not isinstance(field, relation_types):
                        continue
                    removal = RemoveField(model_name=operation.name, name=field_name)
                    if any(
                        PreparedOperationGraph.get_provided_key(dependency) in needed_keys
                        for dependency in removal.get_provided_dependencies(app_label)
                    ):
                        return position, (app_label, removal)
        return None
