from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Awaitable, Callable, Iterable
from contextlib import nullcontext
from typing import Any, ClassVar

from hare.core.caching.cache import Cache
from hare.core.connections.connections import Connections
from hare.core.log import logger
from hare.dialects.base.client.database_client import DatabaseClient
from hare.exceptions import QueryError
from hare.migrations.constants import (
    LATEST_MIGRATION,
    MISSING_MIGRATION_MESSAGE_TEMPLATE,
)
from hare.migrations.exceptions import TenantMigrationError, UnknownMigrationError
from hare.migrations.execution.constants import ZERO_MIGRATION
from hare.migrations.execution.executor.migration_target import MigrationTarget
from hare.migrations.execution.executor.plan_step import PlanStep
from hare.migrations.execution.executor.rollback_walk import RollbackWalk
from hare.migrations.execution.migration_runner import MigrationRunner
from hare.migrations.execution.swappable_setting_check import SwappableSettingCheck
from hare.migrations.loading.graph.migration_graph import MigrationGraph
from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.migrations.loading.migration_loader import MigrationLoader
from hare.migrations.loading.recorder.migration_recorder import MigrationRecorder
from hare.migrations.migration import Migration
from hare.migrations.safety.migration_risk import MigrationRisk
from hare.migrations.safety.migration_safety_checker import MigrationSafetyChecker
from hare.migrations.state.state import State
from hare.models.tenancy.tenancy import Tenancy
from hare.models.tenancy.tenant_schemas import TenantSchemas
from hare.query.scopes.row_visibility import RowVisibility
from hare.query.scopes.tenants.model_tenants import ModelTenants
from hare.query.scopes.tenants.tenant_values import TenantValues


class MigrationExecutor:
    """Applies and unapplies the migrations of one connection: their plan, the lock and the journal."""

    #: The full forwards plan of an executor's migration graph - kept on the executor in
    #: ``_full_plan_cache``, dropped when the graph is built again.
    full_plans: ClassVar[Cache[Any]] = Cache(holds_sql=False, keyed_by_model=False, owner_attribute="_full_plan_cache")

    def __init__(
        self,
        connection: DatabaseClient,
        apps_config: dict[str, dict[str, Any]],
        *,
        full_apps_config: dict[str, dict[str, Any]] | None = None,
        lock_timeout: float | None = None,
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
            lock_timeout: Seconds a migration's statement may wait for a lock - see
                ``MigrationRunner``.
        """
        self.connection = connection
        self.apps_config = apps_config
        self.recorder = MigrationRecorder(connection)
        self.runner = MigrationRunner(connection, recorder=self.recorder, lock_timeout=lock_timeout)
        self.loader = MigrationLoader(full_apps_config if full_apps_config is not None else apps_config, self.recorder)
        self._full_plan_cache: list[MigrationKey] | None = None
        self._logger = logger

    async def build_graph(self) -> None:
        """Rebuilds ``self.loader.graph`` from disk and drops the cached full plan - every rebuild goes
        through here.
        """
        await self.loader.build_graph()
        self._full_plan_cache = None

    async def migrate(
        self,
        targets: Iterable[MigrationTarget] | None = None,
        *,
        steps: list[PlanStep] | None = None,
        applied: set[MigrationKey] | None = None,
        fake: bool = False,
        dry_run: bool = False,
        progress: Callable[[str, str, str], object] | None = None,
        report: Callable[[list[PlanStep]], Awaitable[object]] | None = None,
    ) -> None:
        """Applies and unapplies migrations to reach the targets.

        Unless it's a dry run, the run holds the dialect's migration lock - the schema editor's
        ``get_migration_lock_sql()`` on a connection of its own, or the client's ``hold_migration_lock()``
        where the database has no lock statement: a second ``migrate`` on the same database waits for it,
        then reads the migrations it applied as applied.

        Args:
            targets: The migrations to reach - every app's latest when None; with ``steps``, what
                the schema of each tenant, with a journal of its own, is migrated to. A one-shot
                iterable is retained for every tenant's plan.
            steps: The steps to run in the shared schema instead of a plan of their own - those
                ``ConnectionsPlan`` gives this connection.
            applied: The migrations applied when the steps run - of every connection; the journal's
                when None.
            fake: Record the migrations as applied or unapplied without running them.
            dry_run: Run the operations without touching the database or the journal.
            progress: Called with each migration's start and end.
            report: Called with the steps about to run in the shared schema, under the lock - the
                ones another migrate ran meanwhile left out.
        """
        lock_sql = (
            None if dry_run else self.connection.dialect.schema_editor_class.table_locks_class.get_migration_lock_sql()
        )
        if lock_sql is None:
            async with nullcontext() if dry_run else self.connection.hold_migration_lock():
                await self._run_all_migrations(
                    targets, steps=steps, applied=applied, fake=fake, dry_run=dry_run, progress=progress, report=report
                )
            return
        # The migrations run in the context from before the lock's transaction opens - inside it the
        # connection alias resolves to that transaction, and every migration would run in it.
        migrations_context = contextvars.copy_context()
        lock_connection = Connections.current().create_independent(self.connection.connection_alias)
        try:
            async with lock_connection._in_transaction() as lock_client:
                self._logger.debug("Taking the migration lock")
                # Run as a script: a statement run with parameters keeps its snapshot until the
                # transaction's next one, and an index built concurrently waits for every older one.
                await lock_client.execute_script(lock_sql)
                await asyncio.create_task(
                    self._run_all_migrations(
                        targets,
                        steps=steps,
                        applied=applied,
                        fake=fake,
                        dry_run=dry_run,
                        progress=progress,
                        report=report,
                    ),
                    context=migrations_context,
                )
        finally:
            await lock_connection.close()

    async def _run_all_migrations(
        self,
        targets: Iterable[MigrationTarget] | None,
        *,
        steps: list[PlanStep] | None,
        applied: set[MigrationKey] | None,
        fake: bool,
        dry_run: bool,
        progress: Callable[[str, str, str], object] | None,
        report: Callable[[list[PlanStep]], Awaitable[object]] | None,
    ) -> None:
        """Runs ``migrate``'s plan on the connection - with ``tenant_schema_template``, first in the
        shared schema, then in each tenant's schema with a journal of its own, inside the tenant's scope
        (the tenant value as text); inside a tenant scope
        only in that tenant's schema.

        Args:
            targets: The migrations to reach.
            steps: The steps to run in the shared schema instead of a plan - a tenant's schema, with a
                journal of its own, plans the targets.
            applied: The migrations applied when the steps run.
            fake: Record without running.
            dry_run: Run without touching the database or the journal.
            progress: Called with each migration's start and end.
            report: Called with the steps about to run in the shared schema.
        """
        has_tenant_schemas = (
            self.connection.tenant_schema_template is not None and self.connection.tenant_schema is None
        )
        if has_tenant_schemas and targets is not None:
            targets = tuple(targets)
        scope = RowVisibility.active_tenant.get()
        if type(scope) is ModelTenants:
            # A scope given model by model migrates as a scope of the one tenant it gives the connection's
            # Meta.tenant_schema models - naming none of them, as no scope.
            schema_tenant = scope.get_schema_tenant(self.connection.connection_alias) if has_tenant_schemas else None
            scope = None if schema_tenant is None else TenantValues(schema_tenant)
        # A scope of several tenants migrates their schemas alone, as a scope of one tenant does its own.
        scoped_schema_names = (
            {TenantSchemas.get_schema_name(self.connection, tenant) for tenant in scope.values}
            if has_tenant_schemas and type(scope) is TenantValues
            else None
        )
        if scoped_schema_names is None:
            await self._run_migrations(
                targets, steps=steps, applied=applied, fake=fake, dry_run=dry_run, progress=progress, report=report
            )
        if not has_tenant_schemas:
            return

        failures: list[tuple[str, Exception]] = []
        for schema_name, tenant in await TenantSchemas.get_schemas(self.connection):
            if scoped_schema_names is not None and schema_name not in scoped_schema_names:
                continue
            tenant_executor = MigrationExecutor(
                self.connection.get_schema_client(schema_name),
                self.apps_config,
                full_apps_config=self.loader.apps_config,
                lock_timeout=self.runner.lock_timeout,
            )
            # The tenant's scope sends every query of the connection - a migration's transaction, its
            # RunPython - to the tenant schema's client.
            try:
                with Tenancy.scope(tenant):
                    await tenant_executor._run_migrations(
                        targets, steps=None, applied=None, fake=fake, dry_run=dry_run, progress=progress, report=None
                    )
            except Exception as error:
                # One tenant's failure leaves the others' schemas to migrate.
                failures.append((schema_name, error))
        if failures:
            listed = "; ".join(f"{schema_name}: {type(error).__name__}: {error}" for schema_name, error in failures)
            raise TenantMigrationError(
                f"The migrations failed in the schemas of {len(failures)} tenant(s) - {listed}. The other "
                "schemas are migrated; the next migrate runs in each schema what its own journal lacks."
            ) from failures[0][1]

    async def _run_migrations(
        self,
        targets: Iterable[MigrationTarget] | None,
        *,
        steps: list[PlanStep] | None,
        applied: set[MigrationKey] | None,
        fake: bool,
        dry_run: bool,
        progress: Callable[[str, str, str], object] | None,
        report: Callable[[list[PlanStep]], Awaitable[object]] | None,
    ) -> None:
        """Runs ``migrate``'s plan - see ``migrate()``.

        Args:
            targets: The migrations to reach.
            steps: The steps to run instead of a plan.
            applied: The migrations applied when they run; the journal's when None.
            fake: Record without running.
            dry_run: Run without touching the database or the journal.
            progress: Called with each migration's start and end.
            report: Called with the plan before it runs.
        """
        self._logger.debug("Building migration graph")
        await self.build_graph()

        if not dry_run:
            self._logger.debug("Ensuring migration schema")
            await self.runner.ensure_journal()

        self._logger.debug("Loading applied migrations")
        # The loader's view - a squashed migration whose replaced migrations are applied counts too.
        journal_applied = set(self.loader.applied_migrations)
        await SwappableSettingCheck(self.connection, self.apps_config, self.loader.disk_migrations).check(
            journal_applied
        )
        if applied is None:
            applied = journal_applied
        else:
            # Another migrate may have run migrations of this connection while this one waited for the
            # lock - its journal, read under the lock, tells; the steps it ran are left out.
            applied = {key for key in applied if key.app_label not in self.apps_config} | journal_applied
            if steps is not None:
                steps = [
                    step
                    for step in steps
                    if (MigrationKey(app_label=step.migration.app_label, name=step.migration.name) in journal_applied)
                    is step.backward
                ]

        self._logger.debug("Building migration plan")
        plan = steps if steps is not None else self.get_migration_plan(targets, applied, self.loader.graph)
        if report is not None:
            await report(plan)

        if not fake:
            # Refused before the first migration is unapplied - the later reversible ones would be
            # undone, their reverse code run, and the run stop half way at an irreversible one.
            for step in plan:
                if step.backward:
                    step.migration.raise_if_irreversible()

        rollback_walk: RollbackWalk | None = None
        state_cache_by_key: dict[MigrationKey, State] | None = None
        state_positions: dict[MigrationKey, int] = {}
        if not fake and any(step.backward for step in plan):
            state_keys = self.get_state_keys(applied)
            if RollbackWalk.fits(plan, state_keys):
                self._logger.debug("Building rollback walk")
                rollback_walk = RollbackWalk.build(self, applied)
            else:
                self._logger.debug("Building rollback state cache")
                state_cache_by_key = await self._project_states_by_key(applied)
                state_positions = {key: position for position, key in enumerate(state_keys)}
        # Where the migrations unapplied so far stood - a cached state after one of them still holds its models.
        unapplied_positions: list[int] = []

        state_cache: State | None = None
        for step in plan:
            key = MigrationKey(app_label=step.migration.app_label, name=step.migration.name)
            if step.backward:
                if fake:
                    if not dry_run:
                        await self.runner.record(step.migration, applied=False)
                elif rollback_walk is not None:
                    undos = rollback_walk.move_to_after(key)
                    self._emit(progress, "rollback_start", key)
                    await self.runner.unapply(step.migration, rollback_walk.state, dry_run=dry_run, undos=undos)
                    self._emit(progress, "rollback_done", key)
                else:
                    position = state_positions.get(key, -1)
                    if state_cache_by_key is not None and not any(
                        unapplied_position < position for unapplied_position in unapplied_positions
                    ):
                        state_before = state_cache_by_key[key]
                    else:
                        # An app depending on this one was unapplied first - its models are gone.
                        state_before = await self._project_state(applied, upto=key)
                    unapplied_positions.append(position)
                    self._emit(progress, "rollback_start", key)
                    await self.runner.unapply(step.migration, state_before, dry_run=dry_run)
                    self._emit(progress, "rollback_done", key)
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
                    await self.runner.record(step.migration, applied=True)
                applied.add(key)
        if not dry_run:
            await self._record_completed_replacements()

    async def _record_completed_replacements(self) -> None:
        """Records each squashed migration whose replaced migrations are all recorded - they were
        applied one by one, and from now on the squashed one stands for them."""
        recorded_keys = set(await self.recorder.applied_migrations())
        for key, migration in self.loader.replacements.items():
            if key in recorded_keys:
                continue
            if all(
                MigrationKey(app_label=app_label, name=name) in recorded_keys for app_label, name in migration.replaces
            ):
                await self.recorder.record_applied(key.app_label, key.name)

    def get_targets_of_steps(self, steps: list[PlanStep]) -> list[MigrationTarget]:
        """The targets whose plan the steps are: each app's last migration applied, the migrations before
        the first one of an app unapplied - its start when it has none.

        Args:
            steps: Steps of one direction.

        Returns:
            The targets.
        """
        last_steps_by_app: dict[str, PlanStep] = {}
        for step in steps:
            last_steps_by_app[step.migration.app_label] = step
        targets: list[MigrationTarget] = []
        node_map = self.loader.graph.node_map
        for app_label, step in last_steps_by_app.items():
            key = MigrationKey(app_label=app_label, name=step.migration.name)
            if not step.backward:
                targets.append(MigrationTarget(app_label=app_label, name=key.name))
                continue
            parents = sorted(parent.key for parent in node_map[key].parents if parent.key.app_label == app_label)
            if not parents:
                targets.append(MigrationTarget(app_label=app_label, name=ZERO_MIGRATION))
            targets.extend(MigrationTarget(app_label=app_label, name=parent.name) for parent in parents)
        return targets

    async def check_safety(self, checker: MigrationSafetyChecker) -> list[MigrationRisk]:
        """Checks every migration ``migrate`` would apply on this connection, in its order, against
        the database - each one on the migration state the ones before it leave.

        Args:
            checker: The migration safety check.

        Returns:
            The risks, migration by migration.
        """
        await self.build_graph()
        applied = set(self.loader.applied_migrations)
        state = await self._project_state(applied)
        risks: list[MigrationRisk] = []
        for step in self.get_migration_plan(None, applied, self.loader.graph):
            risks.extend(
                await checker.check(
                    step.migration,
                    state,
                    dialect=self.connection.dialect,
                    features=self.connection.features,
                    client=self.connection,
                )
            )
            state = await step.migration.apply(state, dry_run=True, schema_editor=None)
        return risks

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
        # Every migration on disk - a squashed one and the ones it replaces alike - is a node, and
        # the state is that of the migration's own dependencies.
        await self.loader.build_graph(replaces_migrations=False)
        self._full_plan_cache = None

        # Resolve migration key — support prefix matching
        key = self._get_migration_key(app_label, migration_name)
        migration = self.loader.graph.nodes[key]
        if not isinstance(migration, Migration):
            raise UnknownMigrationError(MISSING_MIGRATION_MESSAGE_TEMPLATE.format(key=key))

        state = State(default_connections=self.get_default_connections())
        for dependency_key in self.loader.graph.forwards_plan(key)[:-1]:
            dependency = self.loader.graph.nodes[dependency_key]
            if dependency is None:
                raise UnknownMigrationError(MISSING_MIGRATION_MESSAGE_TEMPLATE.format(key=dependency_key))
            await dependency.apply(state, dry_run=True, schema_editor=None)

        return await self.runner.collect_sql(migration, state, backward=backward)

    def _get_migration_key(self, app_label: str, migration_name: str) -> MigrationKey:
        """The key of a migration named in full or by the start of its name."""
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
            names = ", ".join(migration.name for migration in matches)
            raise UnknownMigrationError(
                f"Ambiguous migration name {migration_name!r} for app {app_label!r}. Matches: {names}"
            )
        raise UnknownMigrationError(f"Cannot find migration {migration_name!r} in app {app_label!r}")

    async def _project_state(self, applied: set[MigrationKey], *, upto: MigrationKey | None = None) -> State:
        state = State(default_connections=self.get_default_connections())
        for key in self.get_state_keys(applied):
            if upto and key == upto:
                break
            migration = self.loader.graph.nodes[key]
            if migration is None:
                raise UnknownMigrationError(MISSING_MIGRATION_MESSAGE_TEMPLATE.format(key=key))
            await migration.apply(state, dry_run=True, schema_editor=None)
        return state

    async def _project_states_by_key(self, applied: set[MigrationKey]) -> dict[MigrationKey, State]:
        state = State(default_connections=self.get_default_connections())
        states_by_key: dict[MigrationKey, State] = {}
        for key in self.get_state_keys(applied):
            states_by_key[key] = state.clone()
            migration = self.loader.graph.nodes[key]
            if migration is None:
                raise UnknownMigrationError(MISSING_MIGRATION_MESSAGE_TEMPLATE.format(key=key))
            await migration.apply(state, dry_run=True, schema_editor=None)
        return states_by_key

    def get_default_connections(self) -> dict[str, str]:
        """Each app's default connection alias."""
        return {
            label: config.get("default_connection", "default") for label, config in self.loader.apps_config.items()
        }

    def get_state_keys(self, applied: set[MigrationKey]) -> list[MigrationKey]:
        """The migrations whose changes make the state this connection's migrations run on, in plan
        order - the applied ones, and those of the apps of other connections whose dependencies are
        in it: their tables are in another database, their models are what a relation without a
        constraint (``db_constraint=False``) points to.

        Args:
            applied: The migrations applied on this connection.

        Returns:
            The migrations.
        """
        node_map = self.loader.graph.node_map
        state_keys: list[MigrationKey] = []
        state_key_set: set[MigrationKey] = set()
        for key in self._full_plan():
            if key in applied or (
                key.app_label not in self.apps_config
                and all(parent.key in state_key_set for parent in node_map[key].parents)
            ):
                state_keys.append(key)
                state_key_set.add(key)
        return state_keys

    def _full_plan(self) -> list[MigrationKey]:
        if self._full_plan_cache is not None:
            return list(self._full_plan_cache)
        plan = self.loader.graph.full_forwards_plan()
        MigrationExecutor.full_plans.set_owner_value(self, list(plan))
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

    def get_migration_plan(
        self,
        targets: Iterable[MigrationTarget] | None,
        applied: set[MigrationKey],
        graph: MigrationGraph,
        app_labels: Iterable[str] | None = None,
    ) -> list[PlanStep]:
        """The steps reaching the targets.

        Args:
            targets: The migrations to reach - every app's latest when None.
            applied: The migrations applied.
            graph: The migration graph.
            app_labels: The apps whose steps are planned - this executor's when None.

        Returns:
            The steps, in order.
        """
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
        planned_app_labels = set(app_labels) if app_labels is not None else set(self.apps_config)
        return [step for step in deduped if step.migration.app_label in planned_app_labels]

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
            UnknownMigrationError: The target names no migration of the graph, or begins the names of
                several.
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
            # A unique prefix of a migration's name names it (``0002``), as sqlmigrate takes it.
            key = self._get_migration_key(target.app_label, target.name)
            target = MigrationTarget(app_label=key.app_label, name=key.name)
        if key in applied:
            return self._backward_plan(target, applied, graph)
        return self._forward_plan(target, applied, graph)

    @staticmethod
    def _forward_plan(
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
                raise UnknownMigrationError(MISSING_MIGRATION_MESSAGE_TEMPLATE.format(key=key))
            plan.append(PlanStep(migration=migration, backward=False))
        return plan

    @staticmethod
    def _backward_plan(
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
                raise UnknownMigrationError(MISSING_MIGRATION_MESSAGE_TEMPLATE.format(key=key))
            plan.append(PlanStep(migration=migration, backward=True))
        return plan

    @staticmethod
    def _dedupe_plan(plan: list[PlanStep]) -> list[PlanStep]:
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
