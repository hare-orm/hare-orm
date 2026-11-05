from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from types import TracebackType
from typing import Any

from hare.core.config import HareConfig
from hare.core.constants import DEFAULT_CONNECTION_NAME
from hare.core.hare_context import HareContext
from hare.migrations.api.migration_request_parser import MigrationRequestParser
from hare.migrations.execution.connection_migration_plan import ConnectionMigrationPlan
from hare.migrations.execution.connections_plan import ConnectionsPlan
from hare.migrations.execution.executor.migration_executor import MigrationExecutor
from hare.migrations.execution.executor.plan_step import PlanStep
from hare.migrations.loading.migration_definitions import MigrationDefinitions
from hare.migrations.loading.migration_loader import MigrationLoader
from hare.migrations.loading.migrations_modules import MigrationsModules
from hare.migrations.loading.recorder.noop_recorder import NoopRecorder
from hare.migrations.messages import MIGRATION_SESSION_CLOSE_FAILED
from hare.migrations.state.migration_environment import MigrationEnvironment


class MigrationSession:
    """One migration request's context, selected applications and definition snapshot.

    Entering never replaces a caller's application context or its global fallback.
    Exiting closes only the session's clients and restores the caller's context.
    Executors share definitions and a captured application environment, but read their
    journals independently, including the refresh inside each database's migration lock.
    """

    def __init__(
        self,
        config: Mapping[str, Any] | HareConfig | str,
        app_labels: Sequence[str] | None = None,
        *,
        create_packages: bool = False,
    ) -> None:
        self.config = HareConfig.load(config)
        self.context = HareContext()
        self.app_labels = tuple(app_labels) if app_labels is not None else None
        self.create_packages = create_packages
        self.configured_apps: dict[str, dict[str, Any]] = {}
        self.selected_apps: list[str] = []
        self.apps_by_connection: dict[str, dict[str, dict[str, Any]]] = {}
        self.definitions: MigrationDefinitions | None = None
        self.environment: MigrationEnvironment

    async def __aenter__(self) -> MigrationSession:
        await self.context.__aenter__()
        self.definitions = None
        try:
            config = self.config.to_dict()
            self.configured_apps = (
                MigrationsModules.get_apps_with_packages(config["apps"])
                if self.create_packages
                else MigrationsModules.get_apps_with_existing_modules(config["apps"])
            )
            config["apps"] = self.configured_apps
            self.selected_apps = (
                list(dict.fromkeys(self.app_labels)) if self.app_labels else list(self.configured_apps)
            )
            self.apps_by_connection = MigrationRequestParser.group_selected_apps_by_connection(
                self.configured_apps, self.selected_apps, DEFAULT_CONNECTION_NAME
            )
            await self.context.init(config=config, connect=False)
            # A successful context.init() publishes Apps, even without opening database connections.
            assert self.context.apps is not None  # nosec B101
            self.environment = MigrationEnvironment.from_apps(self.context.apps)
        except BaseException as error:
            await self.__aexit__(type(error), error, error.__traceback__)
            raise
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            await self.context.__aexit__(exception_type, exception, traceback)
        except BaseException as error:
            if exception is not None and error is not exception:
                raise BaseExceptionGroup(MIGRATION_SESSION_CLOSE_FAILED, [exception, error]) from None
            raise

    def get_definitions(self) -> MigrationDefinitions:
        """Read files once in this context; journal refreshes do not replace this snapshot."""
        if self.definitions is None:
            loader = MigrationLoader(self.configured_apps, NoopRecorder())
            loader.load_disk()
            self.definitions = MigrationDefinitions.capture(loader)
        return self.definitions

    def create_executors(self, *, lock_timeout: float | None = None) -> dict[str, MigrationExecutor]:
        """Create one executor per selected connection, using the shared snapshot."""
        definitions = self.get_definitions()
        return {
            alias: MigrationExecutor(
                self.context.get_connection(alias),
                apps,
                full_apps_config=self.configured_apps,
                lock_timeout=lock_timeout,
                definitions=definitions,
                environment=self.environment,
                apps=self.context.apps,
            )
            for alias, apps in self.apps_by_connection.items()
        }

    async def plan(self, target: str | None = None) -> list[ConnectionMigrationPlan]:
        """Read current journals and return structured steps for this session's definitions."""
        targets = MigrationRequestParser.parse_targets(target, self.selected_apps)
        return await ConnectionsPlan(self.create_executors()).get_plan(targets)

    async def migrate(
        self,
        target: str | None = None,
        *,
        fake: bool = False,
        dry_run: bool = False,
        reporter: Callable[[str, list[PlanStep], bool, bool], object] | None = None,
        progress: Callable[[str, str, str], object] | None = None,
        lock_timeout: float | None = None,
    ) -> list[ConnectionMigrationPlan]:
        """Apply the request and return reported shared-schema runs in execution order.

        Each executor refreshes its journal under its migration lock. A dry run returns
        planned runs; fake mode updates only the journal. Tenant execution remains part
        of the operation and propagates its failures, but is not a separate plan record.
        """
        if lock_timeout is None and self.config.migrations is not None:
            lock_timeout = self.config.migrations.lock_timeout
        targets = MigrationRequestParser.parse_targets(target, self.selected_apps)
        return await ConnectionsPlan(self.create_executors(lock_timeout=lock_timeout)).migrate(
            targets, fake=fake, dry_run=dry_run, reporter=reporter, progress=progress
        )
