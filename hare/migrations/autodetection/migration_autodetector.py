from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from hare.core.apps import Apps
from hare.core.log import logger
from hare.exceptions import ConfigurationError
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.fields.relations.swappable_model_reference import SwappableModelReference
from hare.migrations.autodetection.diffs.state_field_diff import StateFieldDiff
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.constants import (
    FIRST_MIGRATION,
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
    AddIndex,
    CreateModel,
    DeleteModel,
    HareOperation,
    RemoveField,
    RenameModel,
)
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.migrations.swappable_dependency import SwappableDependency
from hare.migrations.writer.migration_writer import MigrationWriter

if TYPE_CHECKING:
    from hare.migrations.operations.fields.field_like import FieldLike


class MigrationAutodetector:
    def __init__(
        self,
        apps: Apps,
        apps_config: dict[str, dict[str, Any]],
        *,
        now: Callable[[], dt.datetime] | None = None,
        target_app_labels: list[str] | None = None,
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
        """
        self.apps = apps
        self.apps_config = apps_config
        self._now = now or dt.datetime.now
        self.loader = MigrationLoader(apps_config, NoopRecorder(), load=False, ignore_no_migrations=True)
        self.target_app_labels = target_app_labels if target_app_labels is not None else list(apps_config)
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
        self.warnings = []
        self.data_loss_warnings = []
        await self.loader.build_graph()
        old_state = await self._project_state()
        self.project_state = old_state.clone()
        new_state = self.current_state(tracked_state=old_state)
        # Two passes: every target app's operations and name first - a cross-app dependency needs
        # the other apps' new migrations known before any is on disk.
        pending: dict[str, tuple[list[HareOperation], str, bool]] = {}
        connection_by_app = {
            app_label: config.get("default_connection") for app_label, config in self.apps_config.items()
        }
        self.moved_models = {}
        for app_label in self.target_app_labels:
            config = self.apps_config[app_label]
            migrations_module = config.get("migrations")
            if not migrations_module:
                continue
            operation_generator = OperationGenerator(old_state, new_state, connection_by_app)
            operations = operation_generator.generate(app_labels=[app_label])
            self.moved_models.update(operation_generator.moved_models)
            self.warnings.extend(operation_generator.warnings)
            self.data_loss_warnings.extend(operation_generator.data_loss_warnings)
            if not operations:
                continue
            name, initial = self.migration_name(app_label, old_state, new_state)
            pending[app_label] = (operations, name, initial)

        pending_names = {app_label: name for app_label, (_operations, name, _initial) in pending.items()}
        # A relation closing a cycle among apps migrated for the first time together is pulled out
        # of its CreateModel before dependencies are computed.
        deferred_operations, excluded_relation_fields, deferred_dependency_apps = (
            self._split_cross_app_relation_cycles(pending, pending_names)
        )
        removal_dependency_apps = self._split_cross_app_to_field_removals(
            pending, pending_names, old_state, deferred_operations, deferred_dependency_apps
        )
        relation_pending_names = {
            app_label: name for app_label, name in pending_names.items() if app_label not in removal_dependency_apps
        }

        writers: list[MigrationWriter] = []
        for app_label, (operations, name, initial) in pending.items():
            dependencies = self._dependencies_for_app(
                app_label,
                operations,
                old_state,
                new_state,
                pending_names,
                excluded_relation_fields.get(app_label, set()),
                relation_pending_names,
            )
            dependencies = sorted(
                {
                    *dependencies,
                    *(
                        (referencing_app_label, pending_names[referencing_app_label])
                        for referencing_app_label in removal_dependency_apps.get(app_label, set())
                    ),
                }
            )
            writers.append(
                MigrationWriter(
                    name,
                    app_label,
                    operations,
                    dependencies=dependencies,
                    initial=initial,
                    migrations_module=self.apps_config[app_label]["migrations"],
                )
            )
        for app_label, operations in deferred_operations.items():
            follow_up_dependencies: set[tuple[str, str]] = {(app_label, pending_names[app_label])}
            swappable_dependencies = self._get_operations_swappable_dependencies(operations)
            follow_up_dependencies.update(swappable_dependencies)
            for target_app_label in deferred_dependency_apps.get(app_label, set()):
                if self._only_swappable_relations_into(operations, target_app_label):
                    continue
                target_pending_name = pending_names.get(target_app_label)
                if target_pending_name is not None:
                    follow_up_dependencies.add((target_app_label, target_pending_name))
                else:
                    follow_up_dependencies.update(
                        (dep.app_label, dep.name) for dep in self.leaf_nodes(target_app_label)
                    )
            writers.append(
                MigrationWriter(
                    self._follow_up_migration_name(app_label, pending_names[app_label]),
                    app_label,
                    operations,
                    dependencies=sorted(follow_up_dependencies),
                    initial=False,
                    migrations_module=self.apps_config[app_label]["migrations"],
                )
            )
        self._raise_on_dependency_cycle(writers)
        return writers

    async def write(self) -> list[str]:
        writers = await self.changes()
        return [str(writer.write()) for writer in writers]

    def current_state(self, tracked_state: State | None = None) -> State:
        """The current models' state.

        Args:
            tracked_state: The state the migration files produce - an unmanaged model it holds is
                kept, so switching ``managed`` only alters the model's options.

        Returns:
            The state of every managed model, plus the unmanaged ones ``tracked_state`` holds.
        """
        state = State(models={}, apps=StateApps())
        for app_label, models in self.apps.items():
            for model in models.values():
                model_key = (app_label, model.__name__)
                if model._meta.managed is False and (tracked_state is None or model_key not in tracked_state.models):
                    # Meta.managed = False models are never hare's to create, alter or drop.
                    continue
                state.models[model_key] = ModelState.make_from_model(app_label, model)
        return state

    async def _project_state(self) -> State:
        state = State(models={}, apps=StateApps(), ignores_missing_swappable_relations=True)
        for key in self.loader.graph.full_forwards_plan():
            migration = self.loader.graph.nodes[key]
            if not isinstance(migration, Migration):
                raise UnknownMigrationError(MISSING_MIGRATION_MESSAGE_TEMPLATE.format(key=key))
            await migration.apply(state, dry_run=True, schema_editor=None)
        return state

    def _dependencies_for_app(
        self,
        app_label: str,
        operations: list[HareOperation],
        old_state: State,
        new_state: State,
        pending_names: dict[str, str],
        excluded_relation_fields: set[tuple[str, str]] | None = None,
        relation_pending_names: dict[str, str] | None = None,
    ) -> list[tuple[str, str]]:
        dependencies: set[MigrationKey] = set()
        dependencies.update(self.leaf_nodes(app_label))
        dependencies.update(
            self._relation_dependencies(
                app_label,
                old_state,
                new_state,
                pending_names if relation_pending_names is None else relation_pending_names,
                excluded_relation_fields,
            )
        )
        dependencies.update(self._rename_dependencies(app_label, operations, old_state))
        dependencies.update(self._deletion_dependencies(app_label, operations, old_state, pending_names))
        for operation in operations:
            if isinstance(operation, CreateModel) and operation.state_only:
                # The moved model's table is created by its source app's migrations.
                source_app_label, _source_model_name = self.moved_models[(app_label, operation.name)]
                dependencies.update(self.leaf_nodes(source_app_label))
        return sorted(
            [
                *((dep.app_label, dep.name) for dep in dependencies),
                *self._swappable_relation_dependencies(app_label, new_state, excluded_relation_fields),
            ]
        )

    def _deletion_dependencies(
        self,
        app_label: str,
        operations: list[HareOperation],
        old_state: State,
        pending_names: dict[str, str],
    ) -> set[MigrationKey]:
        """The other apps' migrations that must apply before a ``DeleteModel`` in ``app_label`` - their
        new migrations remove or repoint the references.

        Args:
            app_label: The app checked for a DeleteModel.
            operations: The app's pending operations.
            old_state: The state of the migrations on disk.
            pending_names: The new migration's name of every app in this batch.

        Returns:
            The migration keys to depend on.

        Raises:
            ConfigurationError: A referencing app gets no new migration in this batch.
        """
        dependencies: set[MigrationKey] = set()
        for operation in operations:
            if not isinstance(operation, DeleteModel):
                continue
            deleted_model_reference = f"{app_label}.{operation.name}"
            for (referencing_app, referencing_model_name), model_state in old_state.models.items():
                if referencing_app == app_label:
                    continue
                if not self.apps_config.get(referencing_app, {}).get("migrations"):
                    continue
                referencing_field_names = sorted(
                    field_name
                    for field_name, field in model_state.fields.items()
                    if isinstance(field, RELATION_FIELDS)
                    and self._field_relation_reference(field) == deleted_model_reference
                )
                if not referencing_field_names:
                    continue
                pending_name = pending_names.get(referencing_app)
                if pending_name is None:
                    raise ConfigurationError(
                        f"Can't delete model {deleted_model_reference}: "
                        f"{referencing_app}.{referencing_model_name}.{referencing_field_names[0]} still "
                        f"references it in app {referencing_app!r}'s migrations. Run makemigrations for "
                        f"{referencing_app!r} in the same run, so its migration removing the reference "
                        "is applied first."
                    )
                dependencies.add(MigrationKey(app_label=referencing_app, name=pending_name))
        return dependencies

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

    @classmethod
    def _only_swappable_relations_into(cls, operations: list[HareOperation], target_app_label: str) -> bool:
        """Whether every relation field `operations` add into `target_app_label` is a
        ``swappable()`` one.

        Args:
            operations: A migration's operations.
            target_app_label: The app the relations point into.
        """
        relation_fields = [
            field
            for field in cls._get_operations_relation_fields(operations)
            if (reference := cls._field_relation_reference(field)) is not None
            and reference.split(".", 1)[0] == target_app_label
        ]
        return bool(relation_fields) and all(
            isinstance(field.model_name, SwappableModelReference) for field in relation_fields
        )

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

    @staticmethod
    def _raise_on_dependency_cycle(writers: list[MigrationWriter]) -> None:
        """Refuses a batch whose new migrations depend on each other in a cycle.

        Args:
            writers: every migration of this changes() batch.

        Raises:
            ConfigurationError: The batch's migrations can't be applied in any order.
        """
        pending_keys = {(writer.app_label, writer.name) for writer in writers}
        # A "__first__" dependency (a swappable one) names an app's initial migration.
        first_pending_keys = {
            (writer.app_label, FIRST_MIGRATION): (writer.app_label, writer.name)
            for writer in writers
            if writer.initial
        }
        dependencies_by_key = {
            (writer.app_label, writer.name): [
                first_pending_keys.get((dependency[0], dependency[1]), (dependency[0], dependency[1]))
                for dependency in writer.dependencies
                if tuple(dependency) in pending_keys
                # The loader ignores a "__first__" dependency on the migration's own app.
                or (tuple(dependency) in first_pending_keys and dependency[0] != writer.app_label)
            ]
            for writer in writers
        }
        finished: set[tuple[str, str]] = set()
        for start_key in sorted(dependencies_by_key):
            path: list[tuple[str, str]] = []
            stack: list[tuple[tuple[str, str], int]] = [(start_key, 0)]
            while stack:
                key, next_index = stack.pop()
                if next_index == 0:
                    if key in finished:
                        continue
                    if key in path:
                        cycle = [*path[path.index(key) :], key]
                        raise ConfigurationError(
                            "The new migrations depend on each other in a cycle ("
                            + " -> ".join(f"{app}.{name}" for app, name in cycle)
                            + ") - split the change into two makemigrations runs: first remove or "
                            "repoint the relations, then delete or move their target models."
                        )
                    path.append(key)
                dependencies = dependencies_by_key[key]
                if next_index < len(dependencies):
                    stack.append((key, next_index + 1))
                    stack.append((dependencies[next_index], 0))
                else:
                    path.pop()
                    finished.add(key)

    def _split_cross_app_relation_cycles(
        self,
        pending: dict[str, tuple[list[HareOperation], str, bool]],
        pending_names: dict[str, str],
    ) -> tuple[dict[str, list[HareOperation]], dict[str, set[tuple[str, str]]], dict[str, set[str]]]:
        """Breaks a circular relation dependency among apps all getting their first migration in this
        batch: one relation field per edge is removed from its ``CreateModel`` and returned for a
        follow-up migration. Changes the pending operations in place.

        Args:
            pending: app_label -> (operations, name, initial).
            pending_names: app_label -> migration name.

        Returns:
            The follow-up ``AddField`` operations by app, the ``(model_name, field_name)`` pairs
            removed by app, and the apps each follow-up migration depends on.
        """
        field_targets = {
            app_label: self._pending_relation_field_targets(app_label, operations, pending_names)
            for app_label, (operations, _name, _initial) in pending.items()
        }
        dependency_apps: dict[str, set[str]] = {
            app_label: set(targets) for app_label, targets in field_targets.items()
        }

        in_degree = {app_label: len(targets) for app_label, targets in dependency_apps.items()}
        queue = sorted(app_label for app_label, degree in in_degree.items() if degree == 0)
        placed_app_labels: set[str] = set(queue)
        while queue:
            node = queue.pop(0)
            for app_label, targets in dependency_apps.items():
                if app_label in placed_app_labels or node not in targets:
                    continue
                targets.discard(node)
                in_degree[app_label] -= 1
                if in_degree[app_label] == 0:
                    placed_app_labels.add(app_label)
                    queue.append(app_label)
                    queue.sort()

        deferred_operations: dict[str, list[HareOperation]] = {}
        excluded_relation_fields: dict[str, set[tuple[str, str]]] = {}
        deferred_dependency_apps: dict[str, set[str]] = {}
        remaining = sorted(app_label for app_label in pending if app_label not in placed_app_labels)
        while remaining:
            app_label = remaining.pop(0)
            operations = pending[app_label][0]
            for target_app_label in sorted(dependency_apps[app_label]):
                for operation, model_name, field_name in field_targets[app_label].get(target_app_label, []):
                    deferred_operations.setdefault(app_label, []).extend(
                        self._defer_relation_field(operations, operation, field_name)
                    )
                    excluded_relation_fields.setdefault(app_label, set()).add((model_name, field_name))
                    deferred_dependency_apps.setdefault(app_label, set()).add(target_app_label)
            dependency_apps[app_label].clear()
            placed_app_labels.add(app_label)
            for other_app_label in remaining:
                dependency_apps[other_app_label].discard(app_label)
        return deferred_operations, excluded_relation_fields, deferred_dependency_apps

    def _split_cross_app_to_field_removals(
        self,
        pending: dict[str, tuple[list[HareOperation], str, bool]],
        pending_names: dict[str, str],
        old_state: State,
        deferred_operations: dict[str, list[HareOperation]],
        deferred_dependency_apps: dict[str, set[str]],
    ) -> dict[str, set[str]]:
        """Makes each ``RemoveField`` of a field another app's ``to_field`` relation points at run
        after that app's new migration: the removals move into a follow-up migration depending on
        the referencing apps - unless nothing else is left in the app's migration, which then
        depends on them itself.

        Args:
            pending: app_label -> (operations, name, initial); changed in place.
            pending_names: app_label -> migration name.
            old_state: The state of the migrations on disk.
            deferred_operations: app_label -> follow-up operations; extended in place.
            deferred_dependency_apps: app_label -> apps its follow-up migration depends on; extended
                in place.

        Returns:
            app_label -> referencing apps, for each app whose new migration holds only such
            removals.

        Raises:
            ConfigurationError: A referencing app gets no new migration in this batch.
        """
        removal_dependency_apps: dict[str, set[str]] = {}
        for app_label, (operations, _name, _initial) in pending.items():
            old_model_name_by_new_name = {
                operation.new_name: operation.old_name
                for operation in operations
                if isinstance(operation, RenameModel)
            }
            referencing_app_labels_by_removal: list[tuple[RemoveField, set[str]]] = []
            for operation in operations:
                if not isinstance(operation, RemoveField):
                    continue
                old_key = (app_label, old_model_name_by_new_name.get(operation.model_name, operation.model_name))
                referencing_app_labels = {
                    referencing_key[0]
                    for referencing_key, _referencing_name in OperationGenerator.get_to_field_references(
                        old_state, old_key, operation.name
                    )
                    if referencing_key[0] != app_label
                    and self.apps_config.get(referencing_key[0], {}).get("migrations")
                }
                for referencing_app_label in sorted(referencing_app_labels):
                    if referencing_app_label not in pending_names:
                        raise ConfigurationError(
                            f"Can't remove field {app_label}.{operation.model_name}.{operation.name}: a "
                            f"relation of app {referencing_app_label!r} still points at it through "
                            f"to_field. Run makemigrations for {referencing_app_label!r} in the same "
                            "run, so its migration repointing the relation is applied first."
                        )
                if referencing_app_labels:
                    referencing_app_labels_by_removal.append((operation, referencing_app_labels))
            if not referencing_app_labels_by_removal:
                continue
            all_referencing_app_labels = set().union(
                *(app_labels for _operation, app_labels in referencing_app_labels_by_removal)
            )
            if len(referencing_app_labels_by_removal) == len(operations):
                removal_dependency_apps[app_label] = all_referencing_app_labels
                continue
            for operation, _app_labels in referencing_app_labels_by_removal:
                operations.remove(operation)
                deferred_operations.setdefault(app_label, []).append(operation)
            deferred_dependency_apps.setdefault(app_label, set()).update(all_referencing_app_labels)
        return removal_dependency_apps

    def _pending_relation_field_targets(
        self, app_label: str, operations: list[HareOperation], pending_names: dict[str, str]
    ) -> dict[str, list[tuple[HareOperation, str, str]]]:
        """Each other pending app a relation field in ``operations`` references, with the ``(operation,
        model_name, field_name)`` triples referencing it.

        Args:
            app_label: The app of ``operations``.
            operations: The app's pending operations.
            pending_names: Every app getting a new migration in this batch.

        Returns:
            target_app_label -> the triples.
        """
        targets: dict[str, list[tuple[HareOperation, str, str]]] = {}
        for operation in operations:
            candidates: list[tuple[str, FieldLike]]
            if isinstance(operation, CreateModel):
                model_name = operation.name
                candidates = list(operation.fields)
            elif isinstance(operation, AddField):
                model_name = operation.model_name
                candidates = [(operation.name, operation.field)]
            else:
                continue
            for field_name, field in candidates:
                if field is None or not isinstance(field, RELATION_FIELDS):
                    continue
                reference = self._field_relation_reference(field)
                if reference is None:
                    continue
                target_app_label = reference.split(".", 1)[0]
                if target_app_label == app_label or target_app_label not in pending_names:
                    continue
                targets.setdefault(target_app_label, []).append((operation, model_name, field_name))
        return targets

    @staticmethod
    def _defer_relation_field(
        operations: list[HareOperation], operation: HareOperation, field_name: str
    ) -> list[HareOperation]:
        """Removes `field_name` from a pending CreateModel/AddField, together with the indexes
        over it, for use in a follow-up migration.

        Args:
            operations: the pending operations list `operation` currently belongs to, mutated
                in place when `operation` itself must be removed.
            operation: the CreateModel or AddField currently holding the field.
            field_name: name of the field to remove.
        Returns:
            An AddField operation for the removed field, then AddIndex operations for its indexes.
        """
        if isinstance(operation, CreateModel):
            field, indexes = operation.pop_field(field_name)
            return [
                AddField(model_name=operation.name, name=field_name, field=field),
                *(AddIndex(model_name=operation.name, index=index) for index in indexes),
            ]
        if isinstance(operation, AddField):
            operations.remove(operation)
            deferred_operations: list[HareOperation] = [operation]
            for index_operation in list(operations):
                if (
                    isinstance(index_operation, AddIndex)
                    and index_operation.model_name == operation.model_name
                    and (
                        field_name in (index_operation.index.fields or ())
                        or field_name in index_operation.index.include
                    )
                ):
                    operations.remove(index_operation)
                    deferred_operations.append(index_operation)
            return deferred_operations
        raise TypeError(f"Cannot defer field {field_name!r} out of {operation!r}")

    def _follow_up_migration_name(self, app_label: str, original_pending_name: str) -> str:
        match = MIGRATION_NUMBER_RE.match(original_pending_name)
        next_number = int(match.group(1)) + 1 if match else self.next_number(app_label)
        timestamp = self._now().strftime("%Y%m%d_%H%M")
        return f"{next_number:04d}_auto_{timestamp}"

    def _rename_dependencies(
        self, app_label: str, operations: list[HareOperation], old_state: State
    ) -> set[MigrationKey]:
        """The other apps' migrations that must apply before a ``RenameModel`` in ``app_label`` - those
        referencing the model by its old name.

        Args:
            app_label: The app checked for a RenameModel.
            operations: The app's pending operations.
            old_state: The state of the migrations on disk.

        Returns:
            The migration keys to depend on.
        """
        dependencies: set[MigrationKey] = set()
        for operation in operations:
            if not isinstance(operation, RenameModel):
                continue
            old_model_reference = f"{app_label}.{operation.old_name}"
            for (referencing_app, _model_name), model_state in old_state.models.items():
                if referencing_app == app_label:
                    continue
                if not self.apps_config.get(referencing_app, {}).get("migrations"):
                    continue
                references_old_name = any(
                    isinstance(field, RELATION_FIELDS) and self._field_relation_reference(field) == old_model_reference
                    for field in model_state.fields.values()
                )
                if references_old_name:
                    dependencies.update(self.leaf_nodes(referencing_app))
        return dependencies

    @staticmethod
    def _field_relation_reference(
        field: ForeignKeyFieldInstance[Any] | OneToOneFieldInstance[Any] | ManyToManyFieldInstance[Any],
    ) -> str | None:
        model_name = SwappableModelReference.get_model_reference(field.model_name)
        if model_name is None or isinstance(model_name, str):
            return model_name
        related_app_label = model_name._meta.app
        if related_app_label is None:
            return None
        return f"{related_app_label}.{model_name.__name__}"

    def _relation_dependencies(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        pending_names: dict[str, str],
        excluded_relation_fields: set[tuple[str, str]] | None = None,
    ) -> set[MigrationKey]:
        excluded = excluded_relation_fields or set()
        dependencies: set[MigrationKey] = set()
        for (model_app, _model_name), model_state in new_state.models.items():
            if model_app != app_label:
                continue
            for field_name, field in model_state.fields.items():
                if (model_state.name, field_name) in excluded:
                    continue
                if not isinstance(field, RELATION_FIELDS) or isinstance(field.model_name, SwappableModelReference):
                    continue
                reference = self._field_relation_reference(field)
                if reference is None:
                    continue
                related_app, _ = reference.split(".", 1)
                if related_app == app_label:
                    continue
                if not self.apps_config.get(related_app, {}).get("migrations"):
                    continue
                pending_name = pending_names.get(related_app)
                if pending_name is not None and self._is_unchanged_relation_to_existing_model(
                    old_state, (model_app, model_state.name), field_name, field, reference
                ):
                    # Already valid against the related app's migrations on disk - depending on
                    # its new migration too could close a cycle with one depending on this app.
                    pending_name = None
                if pending_name is not None:
                    # The related app gets a new migration in this same batch - that one is the
                    # dependency, not what is on disk.
                    dependencies.add(MigrationKey(app_label=related_app, name=pending_name))
                else:
                    dependencies.update(self.leaf_nodes(related_app))
        return dependencies

    @staticmethod
    def _swappable_relation_dependencies(
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

    @staticmethod
    def _is_unchanged_relation_to_existing_model(
        old_state: State,
        model_key: tuple[str, str],
        field_name: str,
        field: ForeignKeyFieldInstance[Any] | OneToOneFieldInstance[Any] | ManyToManyFieldInstance[Any],
        reference: str,
    ) -> bool:
        """Whether a relation of the new state already exists, unchanged, in the migration files.

        Args:
            old_state: State computed from every migration currently on disk.
            model_key: (app, model name) of the model declaring the relation.
            field_name: The relation field's name.
            field: The relation field of the new state.
            reference: The relation's "app.Model" target.

        Returns:
            True when the target model and an identical field both exist in `old_state`.
        """
        target_key = cast("tuple[str, str]", tuple(reference.split(".", 1)))
        old_model_state = old_state.models.get(model_key)
        if target_key not in old_state.models or old_model_state is None:
            return False
        old_field = old_model_state.fields.get(field_name)
        if old_field is None:
            return False
        old_signature, new_signature = StateFieldDiff.get_comparable_signatures(old_field, field)
        return old_signature == new_signature

    def leaf_nodes(self, app_label: str) -> list[MigrationKey]:
        try:
            nodes = list(self.loader.graph.leaf_nodes(app_label))
        except Exception:
            logger.debug("leaf_nodes(%s) failed, falling back to disk migrations", app_label, exc_info=True)
            nodes = []
        if nodes:
            return nodes
        disk_nodes = sorted([key for key in self.loader.disk_migrations if key.app_label == app_label])
        if disk_nodes:
            return disk_nodes
        names = self._disk_migration_names(app_label)
        if names:
            return [MigrationKey(app_label=app_label, name=name) for name in names]
        return []

    def _disk_migration_names(self, app_label: str) -> list[str]:
        module_name = self.apps_config.get(app_label, {}).get("migrations")
        if not module_name:
            return []
        try:
            path = MigrationWriter.module_path(module_name)
        except Exception:
            logger.debug("module_path(%s) failed, treating as no migrations on disk", module_name, exc_info=True)
            return []
        names: list[str] = []
        for entry in path.iterdir():
            if not entry.is_file() or entry.suffix != ".py":
                continue
            name = entry.stem
            if name == "__init__" or name[0] in "_~":
                continue
            names.append(name)
        return sorted(names)

    def migration_name(self, app_label: str, old_state: State, new_state: State) -> tuple[str, bool]:
        new_has_models = any(key[0] == app_label for key in new_state.models)
        has_migrations = any(key.app_label == app_label for key in self.loader.graph.nodes)
        if not has_migrations and new_has_models:
            return "0001_initial", True
        next_number = self.next_number(app_label)
        timestamp = self._now().strftime("%Y%m%d_%H%M")
        return f"{next_number:04d}_auto_{timestamp}", False

    def next_number(self, app_label: str) -> int:
        numbers = []
        for key in self.loader.graph.nodes:
            if key.app_label != app_label:
                continue
            match = MIGRATION_NUMBER_RE.match(key.name)
            if match:
                numbers.append(int(match.group(1)))
        if not numbers:
            for name in self._disk_migration_names(app_label):
                match = MIGRATION_NUMBER_RE.match(name)
                if match:
                    numbers.append(int(match.group(1)))
        if not numbers:
            return 1
        return max(numbers) + 1
