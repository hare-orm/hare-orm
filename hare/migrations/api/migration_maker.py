from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.exceptions import ConfigurationError
from hare.migrations.api.migration_changes import MigrationChanges
from hare.migrations.api.squashed_migration import SquashedMigration
from hare.migrations.autodetection.autodetector import MigrationAutodetector
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.loading.loader import MigrationLoader
from hare.migrations.loading.recorder.noop_recorder import NoopRecorder
from hare.migrations.migration import Migration
from hare.migrations.operations import RunPython, RunSQL
from hare.migrations.state.apps import StateApps
from hare.migrations.state.project.state import State
from hare.migrations.swappable import SwappableDependency
from hare.migrations.writer.migration_writer import MigrationWriter

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.core.apps import Apps
    from hare.migrations.loading.graph.migration_graph import MigrationGraph
    from hare.migrations.loading.graph.migration_key import MigrationKey


class MigrationMaker:
    """Builds the migrations of ``makemigrations()`` and ``squashmigrations()`` for the models of a
    set-up context.

    Args:
        apps: The context's models.
        apps_config: Every configured app, its migrations package resolved.
    """

    def __init__(self, apps: Apps, apps_config: dict[str, dict[str, Any]]) -> None:
        self.apps = apps
        self.apps_config = apps_config

    async def make(
        self, target_app_labels: list[str], *, empty: bool, merge: bool, name: str | None
    ) -> MigrationChanges:
        """The migrations taking the target apps' migration history to their models.

        Args:
            target_app_labels: The apps to make migrations for.
            empty: Make one empty migration per app.
            merge: Make one migration per app merging its forked history.
            name: The name the migrations get instead of a generated one.

        Returns:
            The changes.

        Raises:
            ConfigurationError: A target app's history has forked (without ``merge``), or has
                nothing to merge, or its branches can't be merged.
        """
        autodetector = MigrationAutodetector(self.apps, self.apps_config, target_app_labels=target_app_labels)
        changes = MigrationChanges()
        if empty:
            await autodetector.loader.build_graph()
            changes.writers = await self.get_empty_writers(autodetector, target_app_labels)
        elif merge:
            await autodetector.loader.build_graph()
            changes.writers = await self.get_merge_writers(autodetector, target_app_labels)
        else:
            await self.raise_on_forked_history(target_app_labels)
            changes.writers = await autodetector.changes()
            changes.warnings = list(autodetector.warnings)
            changes.data_loss_warnings = list(autodetector.data_loss_warnings)
        if name and changes.writers:
            MigrationMaker.rename_writers(changes.writers, name)
        return changes

    async def get_empty_writers(
        self, autodetector: MigrationAutodetector, target_app_labels: list[str]
    ) -> list[MigrationWriter]:
        """One empty migration per app, after its latest ones."""
        old_state = await autodetector._project_state()
        new_state = autodetector._current_state(tracked_state=old_state)
        writers = []
        for label in target_app_labels:
            migrations_module_name = self.apps_config[label].get("migrations")
            if not isinstance(migrations_module_name, str):
                continue
            dependencies = sorted([(key.app_label, key.name) for key in autodetector._leaf_nodes(label)])
            migration_name, initial = autodetector._migration_name(label, old_state, new_state)
            writers.append(
                MigrationWriter(
                    migration_name,
                    label,
                    [],
                    dependencies=dependencies,
                    initial=initial,
                    migrations_module=migrations_module_name,
                )
            )
        return writers

    async def raise_on_forked_history(self, target_app_labels: list[str]) -> None:
        """Refuses making a migration for an app whose history has several heads.

        Raises:
            ConfigurationError: A target app has more than one leaf migration.
        """
        loader = MigrationLoader(self.apps_config, NoopRecorder(), load=False)
        await loader.build_graph()
        for app_label in target_app_labels:
            try:
                loader.graph.get_single_leaf(app_label)
            except ConfigurationError as exc:
                raise ConfigurationError(f"{exc} Run `makemigrations --merge {app_label}` to merge them.") from None

    async def get_merge_writers(
        self, autodetector: MigrationAutodetector, target_app_labels: list[str]
    ) -> list[MigrationWriter]:
        """One empty migration per app depending on every head of its forked history.

        Raises:
            ConfigurationError: An app's history has a single head, or applying its branches
                together fails - they made incompatible changes.
        """
        graph = autodetector.loader.graph
        writers: list[MigrationWriter] = []
        for label in target_app_labels:
            migrations_module_name = self.apps_config[label].get("migrations")
            if not isinstance(migrations_module_name, str):
                continue
            try:
                graph.get_single_leaf(label)
            except ConfigurationError:
                pass
            else:
                raise ConfigurationError(f"Nothing to merge for app '{label}': migration history has a single head")
            leaf_keys = autodetector._leaf_nodes(label)
            try:
                await MigrationMaker.apply_branches_together(graph, leaf_keys)
            except Exception as exc:
                raise ConfigurationError(
                    f"Can't merge migration history for app '{label}': applying both forked "
                    f"branches together fails ({exc}) - the branches made incompatible changes "
                    "(e.g. the same field renamed or altered differently on each side) and can't "
                    "be combined automatically. Resolve the conflict by hand."
                ) from None
            dependencies = sorted((key.app_label, key.name) for key in leaf_keys)
            number = autodetector._next_number(label)
            timestamp = autodetector._now().strftime("%Y%m%d_%H%M")
            writers.append(
                MigrationWriter(
                    f"{number:04d}_merge_{timestamp}",
                    label,
                    [],
                    dependencies=dependencies,
                    initial=False,
                    migrations_module=migrations_module_name,
                )
            )
        return writers

    @staticmethod
    async def apply_branches_together(graph: MigrationGraph, leaf_keys: list[MigrationKey]) -> None:
        """Replays every migration of the forked branches against one state, as ``migrate`` will
        once the merge migration joins them - a conflict (the same field renamed or altered
        differently on each branch) raises here, before the merge migration is written.

        Args:
            graph: The migration graph.
            leaf_keys: The heads of the branches.
        """
        combined_plan: list[MigrationKey] = []
        seen: set[MigrationKey] = set()
        for leaf_key in leaf_keys:
            for key in graph.forwards_plan(leaf_key):
                if key not in seen:
                    seen.add(key)
                    combined_plan.append(key)
        state = State(models={}, apps=StateApps())
        for key in combined_plan:
            migration = graph.nodes[key]
            if isinstance(migration, Migration):
                await migration.apply(state, dry_run=True, schema_editor=None)

    @staticmethod
    def rename_writers(writers: list[MigrationWriter], name: str) -> None:
        """Gives every writer the name, keeping its number, and rewrites the dependencies between
        them.

        Args:
            writers: The writers made together.
            name: The name.
        """
        new_name_by_old_key: dict[tuple[str, str], str] = {}
        for writer in writers:
            try:
                number = int(writer.name.split("_", 1)[0])
            except ValueError:
                number = 1
            new_name = MigrationWriter.format_name(number, name)
            new_name_by_old_key[(writer.app_label, writer.name)] = new_name
            writer.name = new_name
        for writer in writers:
            writer.dependencies = [
                dependency
                if isinstance(dependency, SwappableDependency)
                else (dependency[0], new_name_by_old_key.get((dependency[0], dependency[1]), dependency[1]))
                for dependency in writer.dependencies
            ]

    async def squash(self, app_label: str, name: str | None) -> SquashedMigration:
        """One migration creating an app's models from nothing, replacing all its migrations - see
        ``squashmigrations()``.

        Args:
            app_label: The app.
            name: The squashed migration's name.

        Returns:
            The squashed migration.

        Raises:
            ConfigurationError: The app has no migrations.
        """
        autodetector = MigrationAutodetector(self.apps, self.apps_config)
        await autodetector.loader.build_graph()
        old_names = sorted(key.name for key in autodetector.loader.disk_migrations if key.app_label == app_label)
        if not old_names:
            raise ConfigurationError(f"No migrations found for app '{app_label}' to squash")
        if len(old_names) == 1:
            return SquashedMigration(writer=None, replaced_names=old_names)
        # A RunPython/RunSQL changes no model state - the diff below can't carry it over.
        data_migration_names = [
            key.name
            for key, migration in autodetector.loader.disk_migrations.items()
            if key.app_label == app_label and any(isinstance(op, (RunPython, RunSQL)) for op in migration.operations)
        ]
        new_state = autodetector._current_state()
        empty_state = State(models={}, apps=StateApps())
        operations = OperationGenerator(empty_state, new_state).generate(app_labels=[app_label])
        dependencies = sorted(
            (dependency.app_label, dependency.name)
            for dependency in autodetector._relation_dependencies(app_label, empty_state, new_state, {})
        )
        writer = MigrationWriter(
            MigrationWriter.format_name(autodetector._next_number(app_label), name or "squashed"),
            app_label,
            operations,
            dependencies=dependencies,
            replaces=[(app_label, old_name) for old_name in old_names],
            initial=True,
            migrations_module=self.apps_config[app_label]["migrations"],
        )
        return SquashedMigration(writer=writer, replaced_names=old_names, data_migration_names=data_migration_names)
