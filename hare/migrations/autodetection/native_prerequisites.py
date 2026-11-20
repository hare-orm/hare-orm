from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import replace

from hare.migrations.operations.dependencies.constants import DATABASE_DEPENDENCY_SUBJECTS
from hare.migrations.operations.dependencies.operation_dependency import OperationDependency
from hare.migrations.operations.enums import DependencyType
from hare.migrations.operations.hare_operation import HareOperation
from hare.migrations.state.state import State


class NativePrerequisites:
    """One generation's native resource publication, separate from model comparisons.

    A sibling app sees definitions already emitted on its database. Publications
    also retain pending removals so two consumers cannot emit duplicate DROP commands.
    These are facts of this generation, never cached across later model changes.
    """

    def __init__(self, source: State, connection_by_app: Mapping[str, str | None]) -> None:
        self.state = State(
            default_connections={app: alias for app, alias in connection_by_app.items() if alias is not None},
            extensions=set(source.extensions),
            schemas=set(source.schemas),
            enum_types=deepcopy(source.enum_types),
            native_providers=dict(source.native_providers),
            environment=source.environment,
        )
        self.provided: set[OperationDependency] = set()

    def publish(self, app_label: str, operations: Sequence[HareOperation]) -> None:
        """Publish native state only; another app must still compare against the original models."""
        alias = self.state.default_connections.get(app_label)
        for operation in operations:
            dependencies = [
                dependency.for_database(alias)
                for dependency in operation.get_provided_dependencies(app_label, self.state)
                if dependency.subject in DATABASE_DEPENDENCY_SUBJECTS
            ]
            if dependencies:
                operation.state_forward(app_label, self.state)
                self.provided.update(dependencies)

    def add_change_dependencies(self, operations_by_app: dict[str, list[HareOperation]], source: State) -> None:
        """Order generated consumers after the one published native definition change."""
        changes = {dependency for dependency in self.provided if dependency.dependency_type is DependencyType.CHANGED}
        if not changes:
            return
        for app, operations in operations_by_app.items():
            alias = self.state.default_connections.get(app)
            for operation in operations:
                provided = {
                    dependency.for_database(alias)
                    for dependency in operation.get_provided_dependencies(app, self.state)
                }
                additional = []
                for requirement in operation.get_required_dependencies(app, source):
                    if requirement.subject not in DATABASE_DEPENDENCY_SUBJECTS:
                        continue
                    change = replace(requirement.for_database(alias), dependency_type=DependencyType.CHANGED)
                    if change in changes and change not in provided:
                        additional.append(change)
                operation.generation_dependencies = (*operation.generation_dependencies, *additional)
