from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from hare.core.apps import Apps
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.fields.relations.swappable_model_reference import SwappableModelReference
from hare.migrations.autodetection.migration_splitter import MigrationSplitter
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.autodetection.rename_questions import RenameQuestions
from hare.migrations.constants import (
    MIGRATION_NUMBER_RE,
    MISSING_MIGRATION_MESSAGE_TEMPLATE,
    RELATION_FIELDS,
)
from hare.migrations.exceptions import UnknownMigrationError
from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.migrations.loading.migration_loader import MigrationLoader
from hare.migrations.loading.recorder.noop_recorder import NoopRecorder
from hare.migrations.migration import Migration
from hare.migrations.operations import (
    AddField,
    CreateModel,
    HareOperation,
)
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.migrations.swappable_dependency import SwappableDependency
from hare.migrations.writer.migration_writer import MigrationWriter

if TYPE_CHECKING:
    from hare.migrations.autodetection.split_migration import SplitMigration


class MigrationAutodetector:
    """Compares the models with the state the migrations on disk leave and writes the migrations leading from one
    to the other."""

    def __init__(
        self,
        apps: Apps,
        apps_config: dict[str, dict[str, Any]],
        *,
        now: Callable[[], dt.datetime] | None = None,
        target_app_labels: list[str] | None = None,
        rename_questions: RenameQuestions | None = None,
    ) -> None:
        """Prepares the detection of changes between the migrations and the models.

        Args:
            apps: The models' current state.
            apps_config: Every configured app - a dependency may name another app's migration, which
                must be loaded.
            now: Returns the time a new migration's name is stamped with - ``datetime.datetime.now``
                by default.
            target_app_labels: The apps to detect changes for - every app in ``apps_config`` by
                default.
            rename_questions: The answers whether a removed field, model or app's model is renamed
                or moved - none renamed without them.
        """
        self.apps = apps
        self.apps_config = apps_config
        self._now = now or dt.datetime.now
        self.loader = MigrationLoader(apps_config, NoopRecorder(), ignore_no_migrations=True)
        self.target_app_labels = target_app_labels if target_app_labels is not None else list(apps_config)
        self.rename_questions = rename_questions or RenameQuestions()
        #: The advisories of every diffed app, printed by makemigrations.
        self.warnings: list[str] = []
        #: Populated by changes() - collects OperationGenerator.data_loss_warnings (see
        #: StateFieldDiff's own docstring for what these are) across every target app diffed in
        #: that call.
        self.data_loss_warnings: list[str] = []
        #: Populated by changes() - (app, model) -> (app, model) it was moved from, for every model
        #: moved between apps with its table kept, across every target app diffed in that call.
        self.moved_models: dict[tuple[str, str], tuple[str, str]] = {}
        #: Populated by changes() - the migration state the existing migrations leave, which the new
        #: ones start from.
        self.project_state: State | None = None

    async def changes(self) -> list[MigrationWriter]:
        """The new migrations - each target app's operations, ordered together with the other apps' and
        cut into migrations (``MigrationSplitter``), not written yet.

        Returns:
            One writer per new migration.

        Raises:
            ConfigurationError: An operation needs a change of an app with no new migration, or the new
                migrations depend on each other in a cycle.
        """
        self.warnings = []
        self.data_loss_warnings = []
        await self.loader.build_graph()
        old_state = await self._project_state()
        self.project_state = old_state.clone()
        new_state = self.current_state(tracked_state=old_state)
        connection_by_app = {
            app_label: config.get("default_connection") for app_label, config in self.apps_config.items()
        }
        self.moved_models = {}
        operations_by_app: dict[str, list[HareOperation]] = {}
        for app_label in self.target_app_labels:
            if not self.apps_config[app_label].get("migrations"):
                continue
            operation_generator = OperationGenerator(old_state, new_state, connection_by_app, self.rename_questions)
            operations = operation_generator.generate(app_labels=[app_label])
            self.moved_models.update(operation_generator.moved_models)
            self.warnings.extend(operation_generator.warnings)
            self.data_loss_warnings.extend(operation_generator.data_loss_warnings)
            self.rename_questions.note_renames(app_label, operations)
            if operations:
                operations_by_app[app_label] = operations
        self.rename_questions.raise_if_renames_unapplied()
        apps_with_migrations = [
            app_label for app_label, config in self.apps_config.items() if config.get("migrations")
        ]
        split_migrations = MigrationSplitter(old_state, apps_with_migrations, self.get_leaf_migration_keys).split(
            operations_by_app
        )
        names: list[str] = []
        initial_flags: list[bool] = []
        previous_positions: list[int | None] = []
        last_position_by_app: dict[str, int] = {}
        for position, split_migration in enumerate(split_migrations):
            app_label = split_migration.app_label
            previous_position = last_position_by_app.get(app_label)
            if previous_position is None:
                name, initial = self.migration_name(app_label, new_state)
            else:
                name, initial = self._follow_up_migration_name(app_label, names[previous_position]), False
            names.append(name)
            initial_flags.append(initial)
            previous_positions.append(previous_position)
            last_position_by_app[app_label] = position
        writers: list[MigrationWriter] = []
        for position, split_migration in enumerate(split_migrations):
            app_label = split_migration.app_label
            previous_position = previous_positions[position]
            dependencies: set[tuple[str, str]] = set(split_migration.existing_dependencies)
            if previous_position is None:
                dependencies.update(self.get_leaf_migration_keys(app_label))
                swappable_dependencies = self.get_swappable_dependencies_of_app(
                    app_label, new_state, self.get_later_added_fields(split_migrations, split_migration)
                )
            else:
                dependencies.add((app_label, names[previous_position]))
                swappable_dependencies = self._get_operations_swappable_dependencies(split_migration.operations)
            dependencies.update(swappable_dependencies)
            swappable_app_labels = {dependency[0] for dependency in swappable_dependencies}
            dependencies.update(
                (split_migrations[needed].app_label, names[needed])
                for needed in split_migration.split_dependencies
                # The first migration of the app a swappable setting points into is the swappable dependency
                # itself - named by the setting, so the migration loads where the setting points elsewhere.
                if not (split_migrations[needed].app_label in swappable_app_labels and initial_flags[needed])
            )
            writers.append(
                MigrationWriter(
                    names[position],
                    app_label,
                    split_migration.operations,
                    dependencies=sorted(dependencies),
                    initial=initial_flags[position],
                    migrations_module=self.apps_config[app_label]["migrations"],
                )
            )
        return writers

    def get_leaf_migration_keys(self, app_label: str) -> list[tuple[str, str]]:
        """The ``(app, name)`` of an app's latest existing migrations.

        Args:
            app_label: The app.
        """
        return [(key.app_label, key.name) for key in self.leaf_nodes(app_label)]

    @staticmethod
    def get_later_added_fields(
        split_migrations: list[SplitMigration], first_migration: SplitMigration
    ) -> set[tuple[str, str]]:
        """The ``(model, field)`` of the fields a later new migration of the same app adds - its own
        swappable dependencies are its own.

        Args:
            split_migrations: The new migrations.
            first_migration: An app's first new migration.
        """
        return {
            (operation.model_name, operation.name)
            for split_migration in split_migrations
            if split_migration is not first_migration and split_migration.app_label == first_migration.app_label
            for operation in split_migration.operations
            if isinstance(operation, AddField)
        }

    def current_state(self, tracked_state: State | None = None) -> State:
        """The current models' state.

        Args:
            tracked_state: The state the migration files produce - an unmanaged model it holds is
                kept, so switching ``managed`` only alters the model's options.

        Returns:
            The state of every managed model, plus the unmanaged ones ``tracked_state`` holds.
        """
        state = State()
        for app_label, models in self.apps.items():
            for model in models.values():
                model_key = (app_label, model.__name__)
                if model._meta.managed is False and (tracked_state is None or model_key not in tracked_state.models):
                    # Meta.managed = False models are never hare's to create, alter or drop.
                    continue
                state.models[model_key] = ModelState.make_from_model(app_label, model)
        return state

    async def _project_state(self) -> State:
        state = State(ignores_missing_swappable_relations=True)
        for key in self.loader.graph.full_forwards_plan():
            migration = self.loader.graph.nodes[key]
            if not isinstance(migration, Migration):
                raise UnknownMigrationError(MISSING_MIGRATION_MESSAGE_TEMPLATE.format(key=key))
            await migration.apply(state, dry_run=True, schema_editor=None)
        return state

    @classmethod
    def _get_operations_swappable_dependencies(cls, operations: list[HareOperation]) -> set[SwappableDependency]:
        """The swappable dependencies of the ``swappable()`` relation fields `operations` add.

        Args:
            operations: A migration's operations.

        Returns:
            One dependency per setting.
        """
        return {
            SwappableDependency(field.model_name.setting)
            for field in cls._get_operations_relation_fields(operations)
            if isinstance(field.model_name, SwappableModelReference)
        }

    @staticmethod
    def _get_operations_relation_fields(
        operations: list[HareOperation],
    ) -> list[ForeignKeyFieldInstance[Any] | OneToOneFieldInstance[Any] | ManyToManyFieldInstance[Any]]:
        """The relation fields `operations` create or add.

        Args:
            operations: A migration's operations.
        """
        fields: list[Any] = []
        for operation in operations:
            if isinstance(operation, CreateModel):
                fields.extend(field for _field_name, field in operation.fields)
            elif isinstance(operation, AddField):
                fields.append(operation.field)
        return [field for field in fields if isinstance(field, RELATION_FIELDS)]

    def _follow_up_migration_name(self, app_label: str, original_pending_name: str) -> str:
        match = MIGRATION_NUMBER_RE.match(original_pending_name)
        next_number = int(match.group(1)) + 1 if match else self.next_number(app_label)
        timestamp = self._now().strftime("%Y%m%d_%H%M")
        return f"{next_number:04d}_auto_{timestamp}"

    @staticmethod
    def get_swappable_dependencies_of_app(
        app_label: str, new_state: State, excluded_relation_fields: set[tuple[str, str]] | None = None
    ) -> set[SwappableDependency]:
        """The swappable dependencies of every relation of `app_label`'s models declared with
        ``swappable()`` - written as ``swappable_dependency(setting)``, never as a migration of
        the app the setting happens to point into now.

        Args:
            app_label: The app whose models' relations are checked.
            new_state: The current models' State.
            excluded_relation_fields: (model name, field name) pairs deferred to a follow-up migration.

        Returns:
            One dependency per setting.
        """
        excluded = excluded_relation_fields or set()
        settings = {
            field.model_name.setting
            for (model_app, _model_name), model_state in new_state.models.items()
            if model_app == app_label
            for field_name, field in model_state.fields.items()
            if (model_state.name, field_name) not in excluded
            and isinstance(field, RELATION_FIELDS)
            and isinstance(field.model_name, SwappableModelReference)
        }
        return {SwappableDependency(setting) for setting in settings}

    def leaf_nodes(self, app_label: str) -> list[MigrationKey]:
        """The last migrations of an app in the loaded graph - every migration on disk is in it.

        Args:
            app_label: The app.

        Returns:
            The keys of its leaf migrations.
        """
        return list(self.loader.graph.leaf_nodes(app_label))

    def migration_name(self, app_label: str, new_state: State) -> tuple[str, bool]:
        """The name of the app's next migration - ``0001_initial`` for its first, else the next number
        with the time.

        Args:
            app_label: The app.
            new_state: The state the migration leads to.

        Returns:
            The name, and whether the migration is the app's initial one.
        """
        new_has_models = any(key[0] == app_label for key in new_state.models)
        has_migrations = any(key.app_label == app_label for key in self.loader.graph.nodes)
        if not has_migrations and new_has_models:
            return "0001_initial", True
        next_number = self.next_number(app_label)
        timestamp = self._now().strftime("%Y%m%d_%H%M")
        return f"{next_number:04d}_auto_{timestamp}", False

    def next_number(self, app_label: str) -> int:
        """The number after the highest one of the app's migrations in the loaded graph - 1 for none.

        Args:
            app_label: The app.

        Returns:
            The number.
        """
        numbers = []
        for key in self.loader.graph.nodes:
            if key.app_label != app_label:
                continue
            match = MIGRATION_NUMBER_RE.match(key.name)
            if match:
                numbers.append(int(match.group(1)))
        return max(numbers) + 1 if numbers else 1
