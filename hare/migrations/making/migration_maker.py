from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.core.connections.connections import Connections
from hare.core.constants import DEFAULT_CONNECTION_NAME, DEFAULT_LARGE_TABLE_ROWS
from hare.exceptions import ConfigurationError
from hare.migrations.autodetection.migration_autodetector import MigrationAutodetector
from hare.migrations.loading.migration_loader import MigrationLoader
from hare.migrations.loading.recorder.noop_recorder import NoopRecorder
from hare.migrations.making.migration_changes import MigrationChanges
from hare.migrations.migration import Migration
from hare.migrations.safety.migration_risk import MigrationRisk
from hare.migrations.safety.migration_safety_checker import MigrationSafetyChecker
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.migrations.swappable_dependency import SwappableDependency
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
        large_table_rows: The rows from which a table counts as large for the migration safety
            check of the new migrations.
    """

    def __init__(
        self,
        apps: Apps,
        apps_config: dict[str, dict[str, Any]],
        *,
        large_table_rows: int = DEFAULT_LARGE_TABLE_ROWS,
    ) -> None:
        self.apps = apps
        self.apps_config = apps_config
        self.safety_checker = MigrationSafetyChecker(large_table_rows=large_table_rows)

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
            if changes.writers and autodetector.project_state is not None:
                changes.safety_risks = await self.get_safety_risks(autodetector.project_state, changes.writers)
        if name and changes.writers:
            MigrationMaker.rename_writers(changes.writers, name)
        return changes

    async def get_safety_risks(self, project_state: State, writers: list[MigrationWriter]) -> list[MigrationRisk]:
        """The risky operations of the new migrations, each checked on the state the migrations
        before it leave, by the rules of its app's database - without reading the database.

        Args:
            project_state: The state the existing migrations leave - left as it is.
            writers: The new migrations.

        Returns:
            The risks.
        """
        state = project_state.clone()
        risks: list[MigrationRisk] = []
        for writer in MigrationMaker.get_writers_in_dependency_order(writers):
            migration = Migration(writer.name, writer.app_label, operations=writer.operations)
            migration.atomic = writer.atomic
            connection_alias = self.apps_config[writer.app_label].get("default_connection") or DEFAULT_CONNECTION_NAME
            risks.extend(
                await self.safety_checker.check(migration, state, dialect=Connections.get(connection_alias).dialect)
            )
            await migration.apply(state, dry_run=True, schema_editor=None)
        return risks

    @staticmethod
    def get_writers_in_dependency_order(writers: list[MigrationWriter]) -> list[MigrationWriter]:
        """The new migrations, each after the new ones it depends on.

        Args:
            writers: The new migrations.

        Returns:
            The same migrations, ordered.
        """
        pending = list(writers)
        new_keys = {(writer.app_label, writer.name) for writer in writers}
        done_keys: set[tuple[str, str]] = set()
        ordered: list[MigrationWriter] = []
        while pending:
            ready = [
                writer
                for writer in pending
                if all(
                    tuple(dependency) in done_keys or tuple(dependency) not in new_keys
                    for dependency in writer.dependencies
                )
            ] or pending[:1]
            for writer in ready:
                ordered.append(writer)
                done_keys.add((writer.app_label, writer.name))
                pending.remove(writer)
        return ordered

    async def get_empty_writers(
        self, autodetector: MigrationAutodetector, target_app_labels: list[str]
    ) -> list[MigrationWriter]:
        """One empty migration per app, after its latest ones."""
        old_state = await autodetector._project_state()
        new_state = autodetector.current_state(tracked_state=old_state)
        writers = []
        for label in target_app_labels:
            migrations_module_name = self.apps_config[label].get("migrations")
            if not isinstance(migrations_module_name, str):
                continue
            dependencies = sorted([(key.app_label, key.name) for key in autodetector.leaf_nodes(label)])
            migration_name, initial = autodetector.migration_name(label, old_state, new_state)
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
            except ConfigurationError as error:
                raise ConfigurationError(f"{error} Run `makemigrations --merge {app_label}` to merge them.") from None

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
            leaf_keys = autodetector.leaf_nodes(label)
            try:
                await MigrationMaker.apply_branches_together(graph, leaf_keys)
            except Exception as error:
                raise ConfigurationError(
                    f"Can't merge migration history for app '{label}': applying both forked "
                    f"branches together fails ({error}) - the branches made incompatible changes "
                    "(e.g. the same field renamed or altered differently on each side) and can't "
                    "be combined automatically. Resolve the conflict by hand."
                ) from None
            dependencies = sorted((key.app_label, key.name) for key in leaf_keys)
            number = autodetector.next_number(label)
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
