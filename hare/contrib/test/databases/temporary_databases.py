from __future__ import annotations

import warnings
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import replace
from types import TracebackType
from typing import Any

from hare.contrib.test.databases.resource_cleanup import ResourceCleanup
from hare.contrib.test.databases.reusable_test_databases import ReusableTestDatabases
from hare.contrib.test.databases.test_database_config import TestDatabaseConfig
from hare.contrib.test.databases.test_database_resource import TestDatabaseResource
from hare.contrib.test.messages import TEMPORARY_DATABASES_ACTIVE
from hare.core.config import ConnectionConfig, HareConfig
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError
from hare.warnings import HareLoopSwitchWarning


class TemporaryDatabases:
    """A context whose every connection is a database made for tests: created (or leased from
    ``ReusableTestDatabases``), given the models' tables, and dropped when the block ends - safe
    under xdist. The configured connections themselves are never opened.

    Example::

        async with TemporaryDatabases(config, "postgresql://postgres@localhost/test_{}") as context:
            ...

    Args:
        config: The configuration - its apps, routers and settings; its ``connections`` name the
            aliases only.
        db_url: The URL each connection's database is made from - a "{}" in its database name is
            filled with a fresh name per connection.
        create_databases: False connects to databases another context created and keeps.
        generate_schemas: False skips creating the tables.
        drop_databases: False leaves the databases in place - their owner drops them.
        reuse_databases: Whether a "{}" is filled with a ``ReusableTestDatabases`` slot - reset and
            reused instead of created and dropped. None reads HARE_TEST_REUSE_DATABASES.
        init_options: Further arguments of ``HareContext.init()``.
    """

    def __init__(
        self,
        config: Mapping[str, Any] | HareConfig,
        db_url: str,
        *,
        create_databases: bool = True,
        generate_schemas: bool = True,
        drop_databases: bool = True,
        reuse_databases: bool | None = None,
        **init_options: Any,
    ) -> None:
        self.config = config
        self.db_url = db_url
        self.create_databases = create_databases
        self.generate_schemas = generate_schemas
        self.drop_databases = drop_databases
        self.reuse_databases = ReusableTestDatabases.is_enabled(reuse_databases)
        self.init_options = init_options
        self.context = HareContext()
        self.database_configs: dict[str, TestDatabaseConfig] = {}
        self.resources: list[TestDatabaseResource] = []
        self.cleanup = ResourceCleanup()
        self.ended = False
        self.active = False
        self._exit_stack = ExitStack()

    async def __aenter__(self) -> HareContext:
        if self.active:
            raise ConfigurationError(TEMPORARY_DATABASES_ACTIVE)
        if self.ended:
            self.database_configs.clear()
            self.resources.clear()
            self.cleanup = ResourceCleanup()
            self.ended = False
        self._exit_stack.enter_context(warnings.catch_warnings())
        warnings.filterwarnings("ignore", category=HareLoopSwitchWarning)
        try:
            await self.context.__aenter__()
        except BaseException:
            self._exit_stack.close()
            raise
        self.active = True
        try:
            aliases = self.config.connections if isinstance(self.config, HareConfig) else self.config["connections"]
            for alias in aliases:
                self.database_configs[alias] = TestDatabaseConfig.from_url(
                    self.db_url, reuse=self.reuse_databases, keep_schema=self.generate_schemas
                )
            connections = {alias: database.config for alias, database in self.database_configs.items()}
            config = (
                replace(
                    self.config,
                    connections={alias: ConnectionConfig.from_dict(value) for alias, value in connections.items()},
                )
                if isinstance(self.config, HareConfig)
                else {**self.config, "connections": connections}
            )
            await self.context.init(
                config=config,
                _create_db=False,
                **self.init_options,
            )
            # Initialization retains clients but creates no database. Register a drop
            # only after creation succeeds: a rejected CREATE may name an existing
            # database which this scope must not delete.
            for alias in connections:
                connection = self.context.connections.get_main(alias)
                resource = self.database_configs[alias].bind(connection)
                self.resources.append(resource)
                self.cleanup.push(connection.close)
                if self.create_databases:
                    await resource.create()
                if self.drop_databases:
                    self.cleanup.push(resource.release)
            # A failed table creation must still drop the databases it was creating them in.
            if self.generate_schemas:
                for resource in self.resources:
                    await resource.generate_schema(safe=False)
        except BaseException as error:
            await self._end(type(error), error, error.__traceback__)
            raise
        return self.context

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        await self._end(exception_type, exception, exception_traceback)

    async def _end(self, exception_type: Any, exc_value: Any, traceback: Any) -> None:
        if self.ended:
            return
        self.ended = True
        try:
            try:
                # Registered last, closing pools runs before any database is released.
                self.cleanup.push(self.context.connections.close_all, discard=False)
                await self.cleanup.close()
            finally:
                # A reusable database whose db_delete() never ran goes back to the pool marked for a
                # reset.
                if self.drop_databases:
                    ReusableTestDatabases.release_leases(
                        database.lease for database in self.database_configs.values() if database.lease is not None
                    )
                await self.context.__aexit__(exception_type, exc_value, traceback)
        except BaseException as error:
            self.cleanup.errors.append(error)
        finally:
            self.active = False
            try:
                self._exit_stack.close()
            except BaseException as error:
                self.cleanup.errors.append(error)
        self.cleanup.raise_errors(exc_value)
