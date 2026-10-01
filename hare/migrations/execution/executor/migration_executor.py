import asyncio
import contextvars
from collections.abc import Callable, Iterable
from typing import Any

from hare.core.connections import Connections
from hare.core.log import logger
from hare.dialects.base.client.database_client import DatabaseClient
from hare.exceptions import ConfigurationError, QueryError
from hare.fields.swappable import SwappableModelReference
from hare.migrations.constants import LATEST_MIGRATION, ZERO_MIGRATION
from hare.migrations.exceptions import UnknownMigrationError
from hare.migrations.execution.executor.migration_target import MigrationTarget
from hare.migrations.execution.executor.plan_step import PlanStep
from hare.migrations.execution.runner import MigrationRunner
from hare.migrations.loading.graph.migration_graph import MigrationGraph
from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.migrations.loading.loader import MigrationLoader
from hare.migrations.loading.recorder.migration_recorder import MigrationRecorder
from hare.migrations.migration import Migration
from hare.migrations.operations import CreateModel
from hare.migrations.state.apps import StateApps
from hare.migrations.state.project.state import State
from hare.models import Model


class MigrationExecutor:
    def __init__(
        self,
        connection: DatabaseClient,
        apps_config: dict[str, dict[str, Any]],
        *,
        full_apps_config: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """
        Args:
            connection: This executor's own connection - every migration it actually applies/
                unapplies runs against this one.
            apps_config: The apps THIS connection owns (its own `default_connection` group) -
                only migrations from these apps are ever applied/unapplied/reported by this
                executor, even when the graph below also contains other connections' apps.
            full_apps_config: Every configured app across every connection, used only to build
                the migration graph - a migration in `apps_config` can depend on a concrete
                migration in an app that belongs to a DIFFERENT connection (e.g. an explicit
                `dependencies` entry expressing cross-connection ordering), and
                MigrationLoader.build_graph()'s consistency check needs that other app's
                migrations actually loaded to resolve the reference, not just named. Defaults to
                `apps_config` itself for a caller with only one connection's worth of apps to
                give in the first place (e.g. sqlmigrate(), already passing every configured app
                as `apps_config` directly).
        """
        self.connection = connection
        self.apps_config = apps_config
        self.recorder = MigrationRecorder(connection)
        self.runner = MigrationRunner(connection, recorder=self.recorder)
        self.loader = MigrationLoader(
            full_apps_config if full_apps_config is not None else apps_config, self.recorder, load=False
        )
        self._full_plan_cache: list[MigrationKey] | None = None
        self._logger = logger

    async def _build_graph(self) -> None:
        """Rebuilds ``self.loader.graph`` from disk and drops the cached full plan - every rebuild goes
        through here.
        """
        await self.loader.build_graph()
        self._full_plan_cache = None

    async def migrate(
        self,
        targets: Iterable[MigrationTarget] | None = None,
        *,
        fake: bool = False,
        dry_run: bool = False,
        progress: Callable[[str, str, str], object] | None = None,
    ) -> None:
        """Applies and unapplies migrations to reach the targets.

        Unless it's a dry run, the run holds the dialect's migration lock
        (``Dialect.get_migration_lock_sql``) on a connection of its own: a second ``migrate`` on
        the same database waits for it, then reads the migrations it applied as applied.

        Args:
            targets: The migrations to reach - every app's latest when None.
            fake: Record the migrations as applied or unapplied without running them.
            dry_run: Run the operations without touching the database or the journal.
            progress: Called with each migration's start and end.
        """
        lock_sql = None if dry_run else self.connection.dialect.get_migration_lock_sql()
        if lock_sql is None:
            await self._run_migrations(targets, fake=fake, dry_run=dry_run, progress=progress)
            return
        # The migrations run in the context from before the lock's transaction opens - inside it the
        # connection name resolves to that transaction, and every migration would run in it.
        migrations_context = contextvars.copy_context()
        lock_connection = Connections.current().create_independent(self.connection.connection_name)
        try:
            async with lock_connection._in_transaction() as lock_client:
                self._logger.debug("Taking the migration lock")
                await lock_client.execute(lock_sql)
                await asyncio.create_task(
                    self._run_migrations(targets, fake=fake, dry_run=dry_run, progress=progress),
                    context=migrations_context,
                )
        finally:
            await lock_connection.close()

    async def _run_migrations(
        self,
        targets: Iterable[MigrationTarget] | None,
        *,
        fake: bool,
        dry_run: bool,
        progress: Callable[[str, str, str], object] | None,
    ) -> None:
        """Runs ``migrate``'s plan - see ``migrate()``.

        Args:
            targets: The migrations to reach.
            fake: Record without running.
            dry_run: Run without touching the database or the journal.
            progress: Called with each migration's start and end.
        """
        self._logger.debug("Building migration graph")
        await self._build_graph()

        if not dry_run:
            self._logger.debug("Ensuring migration schema")
            await self.runner.ensure_journal()

        self._logger.debug("Loading applied migrations")
        applied = set(await self.recorder.applied_migrations())
        await self._check_swappable_foreign_keys(applied)

        self._logger.debug("Building migration plan")
        plan = self._migration_plan(targets, applied, self.loader.graph)

        state_cache_by_key: dict[MigrationKey, State] | None = None
        if any(step.backward for step in plan):
            self._logger.debug("Building rollback state cache")
            state_cache_by_key = await self._project_state_cache(applied)

        state_cache: State | None = None
        for step in plan:
            key = MigrationKey(app_label=step.migration.app_label, name=step.migration.name)
            if step.backward:
                if state_cache_by_key is not None:
                    state_before = state_cache_by_key[key]
                else:
                    state_before = await self._project_state(applied, upto=key)
                if not fake:
                    self._emit(progress, "rollback_start", key)
                    await self.runner.unapply(step.migration, state_before, dry_run=dry_run)
                    self._emit(progress, "rollback_done", key)
                elif not dry_run:
                    await self.recorder.record_unapplied(key.app_label, key.name)
                applied.discard(key)
                state_cache = None
            else:
                if state_cache is None:
                    state_cache = await self._project_state(applied)
                if not fake:
                    self._emit(progress, "apply_start", key)
                    state_cache = await self.runner.apply(step.migration, state_cache, dry_run=dry_run)
                    self._emit(progress, "apply_done", key)
                elif not dry_run:
                    await self.recorder.record_applied(key.app_label, key.name)
                applied.add(key)

    async def plan(self, targets: Iterable[MigrationTarget] | None = None) -> list[PlanStep]:
        await self._build_graph()
        applied = set(await self.recorder.applied_migrations())
        return self._migration_plan(targets, applied, self.loader.graph)

    @staticmethod
    def _emit(
        progress: Callable[[str, str, str], object] | None,
        event: str,
        key: MigrationKey,
    ) -> None:
        if progress is not None:
            progress(event, key.app_label, key.name)

    async def collect_sql(
        self,
        app_label: str,
        migration_name: str,
        *,
        backward: bool = False,
    ) -> list[str]:
        """Collect SQL statements for a single migration without executing them.

        Args:
            app_label: The application label.
            migration_name: The migration name (exact or prefix match).
            backward: If True, collect SQL for unapplying the migration.

        Returns:
            A list of SQL strings (including comment annotations).
        """
        await self._build_graph()

        # Resolve migration key — support prefix matching
        key = self._get_migration_key(app_label, migration_name)
        migration = self.loader.graph.nodes[key]
        if not isinstance(migration, Migration):
            raise UnknownMigrationError(f"Missing migration for {key}")

        # For SQL collection we don't need the real database — treat all
        # migrations in the graph as "applied" so _project_state replays
        # them up to the target.
        all_keys = set(self.loader.graph.nodes.keys())
        state = await self._project_state(all_keys, upto=key)

        return await self.runner.collect_sql(migration, state, backward=backward)

    def _get_migration_key(self, app_label: str, migration_name: str) -> MigrationKey:
        """Resolve a migration name to a MigrationKey, supporting prefix matching."""
        exact_key = MigrationKey(app_label=app_label, name=migration_name)
        if exact_key in self.loader.graph.nodes:
            return exact_key

        # Try prefix matching
        matches = [
            key
            for key in self.loader.graph.nodes
            if key.app_label == app_label and key.name.startswith(migration_name)
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            names = ", ".join(m.name for m in matches)
            raise UnknownMigrationError(
                f"Ambiguous migration name {migration_name!r} for app {app_label!r}. Matches: {names}"
            )
        raise UnknownMigrationError(f"Cannot find migration {migration_name!r} in app {app_label!r}")

    async def _check_swappable_foreign_keys(self, applied: set[MigrationKey]) -> None:
        """Refuses to migrate while a table an applied migration created has a foreign key
        declared with ``swappable()`` referencing another table than the one of the model its
        setting points at now - the setting changed after the table was created.

        Args:
            applied: The applied migrations.

        Raises:
            ConfigurationError: Such a foreign key exists.
        """
        if not applied or not self._has_swappable_relations():
            return
        from hare.core.context import HareContext
        from hare.inspectdb.introspector.schema_introspector import SchemaIntrospector
        from hare.migrations.drift.drift_state_builder import DriftStateBuilder

        live_context = HareContext.get_current()
        live_apps = live_context.apps if live_context is not None else None
        if live_apps is None:
            return
        # None stands for the connection's default schema, where a model without Meta.schema lives.
        models_by_schema: dict[str | None, list[type[Model]]] = {}
        for app_label in self.apps_config:
            for model in live_apps.apps.get(app_label, {}).values():
                if model._meta.managed is False or model._meta.swapped is not None:
                    continue
                models_by_schema.setdefault(model._meta.schema, []).append(model)
        mismatch_lines: list[str] = []
        for schema, models in models_by_schema.items():
            existing_tables = set(await SchemaIntrospector.get_table_names(self.connection, schema=schema))
            models = [model for model in models if model._meta.db_table in existing_tables]
            if not models:
                continue
            table_infos = await SchemaIntrospector.inspect_tables(
                self.connection, [model._meta.db_table for model in models], schema=schema, verify_exists=False
            )
            for model, table_info in zip(models, table_infos, strict=True):
                mismatch_lines.extend(
                    f"{model._meta.full_name}.{column_name}: {detail}"
                    for column_name, detail in DriftStateBuilder.get_swappable_foreign_key_mismatches(
                        model._meta.fields_map, table_info
                    )
                )
        if mismatch_lines:
            raise ConfigurationError(
                "A swappable model setting changed after its tables were created:\n  " + "\n  ".join(mismatch_lines)
            )

    def _has_swappable_relations(self) -> bool:
        """Whether a migration on disk of this executor's apps declares a relation with ``swappable()``."""
        for key, migration in self.loader.disk_migrations.items():
            if key.app_label not in self.apps_config:
                continue
            for operation in migration.operations:
                fields = (
                    [field for _name, field in operation.fields]
                    if isinstance(operation, CreateModel)
                    else [getattr(operation, "field", None)]
                )
                if any(isinstance(getattr(field, "model_name", None), SwappableModelReference) for field in fields):
                    return True
        return False

    async def _project_state(self, applied: set[MigrationKey], *, upto: MigrationKey | None = None) -> State:
        default_connections = {
            label: config.get("default_connection", "default") for label, config in self.loader.apps_config.items()
        }
        state = State(models={}, apps=StateApps(default_connections=default_connections))
        for key in self._full_plan():
            if key not in applied:
                continue
            if upto and key == upto:
                break
            migration = self.loader.graph.nodes[key]
            if migration is None:
                raise UnknownMigrationError(f"Missing migration for {key}")
            await migration.apply(state, dry_run=True, schema_editor=None)
        return state

    async def _project_state_cache(self, applied: set[MigrationKey]) -> dict[MigrationKey, State]:
        default_connections = {
            label: config.get("default_connection", "default") for label, config in self.loader.apps_config.items()
        }
        state = State(models={}, apps=StateApps(default_connections=default_connections))
        cache: dict[MigrationKey, State] = {}
        for key in self._full_plan():
            if key not in applied:
                continue
            cache[key] = state.clone()
            migration = self.loader.graph.nodes[key]
            if migration is None:
                raise UnknownMigrationError(f"Missing migration for {key}")
            await migration.apply(state, dry_run=True, schema_editor=None)
        return cache

    def _full_plan(self) -> list[MigrationKey]:
        if self._full_plan_cache is not None:
            return list(self._full_plan_cache)
        plan = self.loader.graph.full_forwards_plan()
        self._full_plan_cache = list(plan)
        return plan

    @staticmethod
    def _default_migration_targets(graph: MigrationGraph) -> list[MigrationTarget]:
        """Every configured app's single latest migration - the targets when none are given.

        Raises:
            ConfigurationError: An app's history forked into several heads with no merge migration.
        """
        targets: list[MigrationTarget] = []
        for app_label in sorted({key.app_label for key in graph.leaf_nodes()}):
            leaf = graph.get_single_leaf(app_label)
            if leaf is not None:
                targets.append(MigrationTarget(app_label=leaf.app_label, name=leaf.name))
        return targets

    def _migration_plan(
        self,
        targets: Iterable[MigrationTarget] | None,
        applied: set[MigrationKey],
        graph: MigrationGraph,
    ) -> list[PlanStep]:
        plan: list[PlanStep] = []
        # Each target is planned against the state the previous targets leave behind, not the
        # database's state before the whole run.
        planned_applied = set(applied)
        target_list = list(targets) if targets is not None else self._default_migration_targets(graph)
        for target in target_list:
            target_steps = self._target_plan(target, planned_applied, graph)
            for step in target_steps:
                key = MigrationKey(app_label=step.migration.app_label, name=step.migration.name)
                if step.backward:
                    planned_applied.discard(key)
                else:
                    planned_applied.add(key)
            plan.extend(target_steps)
        deduped = self._dedupe_plan(plan)
        # The graph may hold other connections' apps as ancestors of this connection's targets -
        # their own executors apply them.
        return [step for step in deduped if step.migration.app_label in self.apps_config]

    def _target_plan(
        self,
        target: MigrationTarget,
        applied: set[MigrationKey],
        graph: MigrationGraph,
    ) -> list[PlanStep]:
        """Plans the steps that bring a single target to its requested state.

        Args:
            target: The migration to migrate to, or an app's LATEST/FIRST marker.
            applied: The migrations applied at the point this target is planned.
            graph: The migration graph.

        Returns:
            The steps for this target alone.

        Raises:
            UnknownMigrationError: The target is not in the graph.
        """
        if target.name == LATEST_MIGRATION:
            # Raises for a forked history with no merge migration.
            leaf = graph.get_single_leaf(target.app_label)
            if leaf is None:
                return []
            leaf_target = MigrationTarget(app_label=leaf.app_label, name=leaf.name)
            return self._forward_plan(leaf_target, applied, graph)
        if target.name == ZERO_MIGRATION:
            steps: list[PlanStep] = []
            for root in graph.root_nodes(target.app_label):
                root_target = MigrationTarget(app_label=root.app_label, name=root.name)
                steps.extend(self._backward_plan(root_target, applied, graph, include_target=True))
            return steps
        key = MigrationKey(app_label=target.app_label, name=target.name)
        if key not in graph.nodes:
            raise UnknownMigrationError(f"Unknown migration target {key}")
        if key in applied:
            return self._backward_plan(target, applied, graph)
        return self._forward_plan(target, applied, graph)

    def _forward_plan(
        self,
        target: MigrationTarget,
        applied: set[MigrationKey],
        graph: MigrationGraph,
    ) -> list[PlanStep]:
        plan: list[PlanStep] = []
        for key in graph.forwards_plan(MigrationKey(app_label=target.app_label, name=target.name)):
            if key in applied:
                continue
            migration = graph.nodes[key]
            if not isinstance(migration, Migration):
                raise UnknownMigrationError(f"Missing migration for {key}")
            plan.append(PlanStep(migration=migration, backward=False))
        return plan

    def _backward_plan(
        self,
        target: MigrationTarget,
        applied: set[MigrationKey],
        graph: MigrationGraph,
        *,
        include_target: bool = False,
    ) -> list[PlanStep]:
        plan: list[PlanStep] = []
        target_key = MigrationKey(app_label=target.app_label, name=target.name)
        if include_target:
            rollback_keys = graph.backwards_plan(target_key)
        else:
            rollback_keys = graph.backwards_plan_for_all(graph.same_app_children(target_key))
        for key in rollback_keys:
            if key not in applied:
                continue
            migration = graph.nodes[key]
            if not isinstance(migration, Migration):
                raise UnknownMigrationError(f"Missing migration for {key}")
            plan.append(PlanStep(migration=migration, backward=True))
        return plan

    def _dedupe_plan(self, plan: list[PlanStep]) -> list[PlanStep]:
        deduped: list[PlanStep] = []
        seen: dict[MigrationKey, bool] = {}
        for step in plan:
            key = MigrationKey(app_label=step.migration.app_label, name=step.migration.name)
            if key in seen:
                if seen[key] != step.backward:
                    raise QueryError(
                        f"Conflicting migration directions for {key} - the requested targets both apply and unapply it"
                    )
                continue
            seen[key] = step.backward
            deduped.append(step)
        return deduped
