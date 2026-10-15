from __future__ import annotations

import warnings
from collections.abc import Mapping
from contextlib import ExitStack
from types import TracebackType
from typing import Any

from hare.contrib.test.databases.reusable_test_databases import ReusableTestDatabases
from hare.core.hare_context import HareContext
from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
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
        config: Mapping[str, Any],
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
        self.lease_number_by_database_name: dict[str, int] = {}
        #: Whether the context was initialized - only then are its databases closed and dropped.
        self.initialized = False
        self._exit_stack = ExitStack()

    async def __aenter__(self) -> HareContext:
        self._exit_stack.enter_context(warnings.catch_warnings())
        warnings.filterwarnings("ignore", category=HareLoopSwitchWarning)
        await self.context.__aenter__()
        try:
            with ReusableTestDatabases.track_new_leases() as lease_number_by_database_name:
                connections = {
                    connection_alias: DbUrlConfigGenerator.expand(
                        self.db_url, testing=True, reuse_databases=self.reuse_databases
                    )
                    for connection_alias in self.config["connections"]
                }
            self.lease_number_by_database_name = lease_number_by_database_name
            await self.context.init(
                config={**self.config, "connections": connections},
                _create_db=self.create_databases,
                **self.init_options,
            )
            self.initialized = True
            # A failed table creation must still drop the databases it was creating them in.
            if self.generate_schemas:
                await self.context.generate_schemas(safe=False)
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
        context = self.context
        try:
            try:
                if self.initialized:
                    await context.connections.close_all(discard=False)
                if self.initialized and self.drop_databases:
                    for connection in context.connections.all():
                        await connection.db_delete()
                        context.connections.discard(connection.connection_alias)
            finally:
                # A reusable database whose db_delete() never ran goes back to the pool marked for a
                # reset.
                if self.drop_databases:
                    ReusableTestDatabases.release_leases(self.lease_number_by_database_name)
                await context.__aexit__(exception_type, exc_value, traceback)
        finally:
            self._exit_stack.close()
