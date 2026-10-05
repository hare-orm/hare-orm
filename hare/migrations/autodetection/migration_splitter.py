from __future__ import annotations

from collections.abc import Callable, Collection
from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import ConfigurationError
from hare.fields.relations.swappable_model_reference import SwappableModelReference
from hare.migrations.autodetection.operation_sorter import OperationSorter
from hare.migrations.autodetection.split_migration import SplitMigration
from hare.migrations.operations import AddField, AlterField, CreateModel
from hare.migrations.operations.dependencies.relation_dependencies import RelationDependencies
from hare.migrations.operations.enums import DependencySubject, DependencyType

if TYPE_CHECKING:
    from hare.migrations.operations import HareOperation
    from hare.migrations.operations.operation import Operation
    from hare.migrations.state.state import State


class MigrationSplitter:
    """Cuts the new operations of every app into migrations: the operations of all apps are ordered
    together (``OperationSorter``), then each goes into its app's current migration - or into a next one
    when it needs a migration of another app that already depends on the current one. A migration
    depends on the new migrations of the operations it needs, and on the existing migrations of an app
    whose model it needs when no new operation creates it.

    Args:
        state: The state the existing migrations leave.
        apps_with_migrations: The apps whose models migrations manage.
        get_leaf_migrations: The ``(app, name)`` of an app's latest existing migrations.
    """

    #: The subjects a migration of another app provides.
    APP_SUBJECTS = frozenset({DependencySubject.MODEL, DependencySubject.FIELD})

    def __init__(
        self,
        state: State,
        apps_with_migrations: Collection[str],
        get_leaf_migrations: Callable[[str], list[tuple[str, str]]],
    ) -> None:
        self.state = state
        self.apps_with_migrations = set(apps_with_migrations)
        self.get_leaf_migrations = get_leaf_migrations
        self.sorter = OperationSorter(state)

    def split(self, operations_by_app: dict[str, list[HareOperation]]) -> list[SplitMigration]:
        """The new migrations.

        Args:
            operations_by_app: Each app's new operations, in the order they were made.

        Returns:
            The migrations; those of one app in the order they run.

        Raises:
            ConfigurationError: An operation needs something of an app none of whose new operations does
                it - its message tells what.
        """
        entries: list[tuple[str, Operation]] = [
            (app_label, operation) for app_label, operations in operations_by_app.items() for operation in operations
        ]
        ordered = self.sorter.sort(entries)
        providers = self.sorter.get_providers(ordered)
        migrations: list[SplitMigration] = []
        current_by_app: dict[str, int] = {}
        migration_by_position: dict[int, int] = {}
        for position, (app_label, operation) in enumerate(ordered):
            needed_migrations: set[int] = set()
            existing_dependencies: set[tuple[str, str]] = set()
            swappable_app_labels = self.get_swappable_app_labels(app_label, operation)
            for requirement in self.sorter.get_required(app_label, operation):
                provider_positions = [
                    provider_position
                    for key in self.sorter.get_provider_keys(requirement)
                    for provider_position in providers.get(key, ())
                    if provider_position != position
                ]
                if provider_positions:
                    needed_migrations.add(migration_by_position[min(provider_positions)])
                    continue
                if (
                    requirement.subject not in self.APP_SUBJECTS
                    or requirement.app_label in ("", app_label)
                    or requirement.app_label not in self.apps_with_migrations
                    or requirement.app_label in swappable_app_labels
                ):
                    continue
                if requirement.dependency_type is not DependencyType.CREATED:
                    raise ConfigurationError(operation.get_unmet_requirement_message(app_label, requirement))
                existing_dependencies.update(self.get_leaf_migrations(requirement.app_label))
            current = current_by_app.get(app_label)
            if current is None or any(
                self.reaches(migrations, needed, current) for needed in needed_migrations if needed != current
            ):
                migration = SplitMigration(app_label)
                if current is not None:
                    migration.split_dependencies.add(current)
                migrations.append(migration)
                current = current_by_app[app_label] = len(migrations) - 1
            migrations[current].operations.append(cast("HareOperation", operation))
            migrations[current].split_dependencies.update(
                needed for needed in needed_migrations if migrations[needed].app_label != app_label
            )
            migrations[current].existing_dependencies.update(existing_dependencies)
            migration_by_position[position] = current
        return migrations

    @staticmethod
    def reaches(migrations: list[SplitMigration], start: int, target: int) -> bool:
        """Whether a migration depends on another, directly or through others.

        Args:
            migrations: The migrations.
            start: The depending migration.
            target: The one it may depend on.
        """
        seen: set[int] = set()
        stack = [start]
        while stack:
            position = stack.pop()
            if position == target:
                return True
            if position in seen:
                continue
            seen.add(position)
            stack.extend(migrations[position].split_dependencies)
        return False

    @staticmethod
    def get_swappable_app_labels(app_label: str, operation: Operation) -> set[str]:
        """The apps the ``swappable()`` relations of an operation point into now - a migration depends on
        their setting, not on a migration of theirs.

        Args:
            app_label: The operation's app.
            operation: The operation.
        """
        fields: list[Any] = []
        if isinstance(operation, CreateModel):
            fields = [field for _field_name, field in operation.fields]
        elif isinstance(operation, (AddField, AlterField)):
            fields = [operation.field]
        app_labels: set[str] = set()
        for field in fields:
            model_name = getattr(field, "model_name", None)
            if isinstance(model_name, SwappableModelReference):
                target_key = RelationDependencies.get_target_key(model_name, app_label)
                if target_key is not None:
                    app_labels.add(target_key[0])
        return app_labels
