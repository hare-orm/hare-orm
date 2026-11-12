from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import warnings
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypeVar

from hare.core.config.connection_config import ConnectionConfig
from hare.core.connections.connection_frame import ConnectionFrame
from hare.core.connections.messages import CONNECTION_HANDLER_CLOSING, CONNECTION_RELEASE_FAILED
from hare.core.constants import CONNECTION_DATABASE_ON_ANOTHER_LOOP_MESSAGE, INITIALIZATION_RELEASE_FAILED
from hare.core.log import logger
from hare.core.resource_completion import ResourceCompletion
from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.dialects.dialect_registry import DialectRegistry
from hare.exceptions import ConfigurationError, QueryError
from hare.instrumentation.enums import PoolRole
from hare.warnings import HareLoopSwitchWarning

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect

    DBConfigType = dict[str, Any]
from hare.core.connections.connection_token import ConnectionToken

TaskResult = TypeVar("TaskResult")


class ConnectionHandler:
    """
    Connection management for a single HareContext.

    Each HareContext owns its own ConnectionHandler instance with isolated storage.
    """

    def __init__(self) -> None:
        self._db_config: DBConfigType | None = None
        self._create_db: bool = False
        self.application = object()
        self.root_clients: dict[str, DatabaseClient] = {}
        self.empty_frame = ConnectionFrame(self.application)
        self.current_frame: contextvars.ContextVar[ConnectionFrame] = contextvars.ContextVar(
            "hare_connection_frame", default=self.empty_frame
        )
        self.stale_connection_closes: set[asyncio.Task[None] | concurrent.futures.Future[None]] = set()
        self.close_task: asyncio.Task[None] | None = None
        self.discard_on_close = False
        self.resource_closers: dict[object, Callable[[], Awaitable[None]]] = {}

    def register_resource(self, resource: object, close: Callable[[], Awaitable[None]]) -> None:
        """Attach a background resource to this application's connection lifetime."""
        if self.close_task is not None and not self.close_task.done():
            raise ConfigurationError(CONNECTION_HANDLER_CLOSING)
        self.resource_closers[resource] = close

    def unregister_resource(self, resource: object) -> None:
        """Detach a resource which has already finished releasing its connection."""
        self.resource_closers.pop(resource, None)

    def _get_storage(self) -> Mapping[str, DatabaseClient]:
        """A read-only view of current clients for diagnostics; it owns no resources."""
        frame = self.get_frame()
        clients = dict(self.root_clients)
        for alias in frame.bindings:
            client = frame.get(alias)
            if client is not None:
                clients[alias] = client
        return MappingProxyType(clients)

    def get_frame(self) -> ConnectionFrame:
        """The task's live frame; a replaced application lifetime has no active bindings."""
        frame = self.current_frame.get()
        return frame if frame.application is self.application else self.empty_frame

    async def _init(self, db_config: DBConfigType, create_db: bool) -> None:
        self._init_config(db_config, create_db)
        await self._init_connections()

    def _init_config(self, db_config: DBConfigType, create_db: bool = False) -> None:
        if self._db_config is None:
            self._db_config = db_config
        else:
            self._db_config.update(db_config)
        self._create_db = create_db

    @property
    def db_config(self) -> DBConfigType:
        """The ``connections`` section of the context's configuration, by alias.

        Raises:
            ConfigurationError: The context isn't initialized, or its connections were closed.
        """
        if self._db_config is None:
            raise ConfigurationError(
                "DB configuration not initialised - Hare.init wasn't called, or close_connections() closed the "
                "connections. Make sure to call Hare.init with a valid configuration before attempting "
                "to create connections."
            )
        return self._db_config

    @staticmethod
    def _get_client_class(db_info: dict[str, Any]) -> type[DatabaseClient]:
        return DialectRegistry.get_driver(db_info.get("engine")).get_client_class(db_info["credentials"])  # type: ignore[arg-type]

    def _get_db_info(self, connection_alias: str) -> str | dict[str, Any]:
        try:
            return self.db_config[connection_alias]
        except KeyError:
            raise ConfigurationError(
                f"Unable to get db settings for alias {connection_alias!r}. Please "
                f"check if the config dict contains this alias and try again"
            )

    async def _init_connections(self) -> None:
        try:
            for connection_alias in self.db_config:
                connection: DatabaseClient = self.get(connection_alias)
                if self._create_db:
                    await connection.db_create()
        except BaseException as error:
            # A failure on a later alias closes the connections already opened for earlier ones.
            try:
                await self.close_all(discard=True)
            except BaseException as release_error:
                raise BaseExceptionGroup(INITIALIZATION_RELEASE_FAILED, [error, release_error]) from None
            finally:
                # A retry must not inherit the broken alias even when release failed.
                self._db_config = None
            raise

    def _get_connection_settings(self, connection_alias: str) -> dict[str, Any]:
        """The settings of a connection - its engine and credentials - a URL expanded.

        Args:
            connection_alias: The connection alias.

        Returns:
            The settings.

        Raises:
            ConfigurationError: The alias doesn't exist.
        """
        db_info = self._get_db_info(connection_alias)
        if isinstance(db_info, Mapping) and "db_url" in db_info:
            db_info = db_info["db_url"]
        if isinstance(db_info, str):
            db_info = DbUrlConfigGenerator.expand(db_info)
        return {**db_info, "credentials": dict(db_info.get("credentials", {}))}

    def get_dialect(self, connection_alias: str) -> Dialect:
        """The dialect of a connection, read off its settings - no client is created.

        Args:
            connection_alias: The connection alias.

        Returns:
            The dialect.

        Raises:
            ConfigurationError: The alias doesn't exist, or its settings name no engine.
        """
        return self._get_client_class(self._get_connection_settings(connection_alias)).dialect

    def _create_connection(
        self,
        connection_alias: str,
        credential_overrides: Mapping[str, Any] | None = None,
        *,
        config: ConnectionConfig | None = None,
    ) -> DatabaseClient:
        db_info = (
            self._get_connection_settings(connection_alias)
            if config is None
            else {"engine": config.engine, "credentials": dict(config.credentials)}
        )
        db_info["credentials"].update(credential_overrides or {})
        effective_config = ConnectionConfig(engine=db_info.get("engine"), credentials=dict(db_info["credentials"]))
        client_class = self._get_client_class(db_info)
        db_parameters = db_info["credentials"].copy()
        db_parameters.update({"connection_alias": connection_alias})
        try:
            connection: DatabaseClient = client_class(**db_parameters)
        except TypeError as error:
            raise ConfigurationError(
                f"Invalid connection parameters for alias {connection_alias!r}: {error}"
            ) from error
        connection.connection_handler = self
        connection.connection_config = effective_config
        return connection

    def get(self, connection_alias: str) -> DatabaseClient:
        """Returns the client queries of ``connection_alias`` run on: its connection (``get_main()``), or -
        for a connection with a schema per tenant - the client of the active tenant's schema
        (``get_tenant_client()``).

        Args:
            connection_alias: The connection alias.

        Raises:
            ConfigurationError: The alias doesn't exist.
        """
        connection = self.get_frame().get(connection_alias)
        if connection is None:
            connection = self.get_main(connection_alias)
        # A connection with a schema per tenant works through the active tenant's client, and a
        # transaction of one with tenant row level security checks the scope stayed the same.
        if connection.tenant_schema_template is not None or connection.tenant_row_level_security:
            return connection.get_tenant_client()
        return connection

    def get_main(self, connection_alias: str) -> DatabaseClient:
        """Returns the connection of ``connection_alias`` itself, creating it if needed - not a tenant
        schema's client. A connection bound to another event loop is replaced, with a
        ``HareLoopSwitchWarning``.

        Args:
            connection_alias: The connection alias.

        Raises:
            ConfigurationError: The alias doesn't exist, or its connection is bound to another event loop
                and holds its database (one living in that connection only) - a new one would open an empty one.
        """
        if self.close_task is not None and not self.close_task.done():
            raise ConfigurationError(CONNECTION_HANDLER_CLOSING)
        storage = self.root_clients
        try:
            connection = storage[connection_alias]
            if not connection._check_loop():
                if connection.holds_database_in_connection():
                    raise ConfigurationError(
                        CONNECTION_DATABASE_ON_ANOTHER_LOOP_MESSAGE.format(connection_alias=connection_alias)
                    )
                warnings.warn(
                    f"Hare connection {connection_alias!r} was created on a different "
                    f"event loop and will be reconnected. If this is expected (e.g., "
                    f"in tests), use hare_test_context() or suppress with: "
                    f"warnings.filterwarnings('ignore', "
                    f"category=HareLoopSwitchWarning)",
                    HareLoopSwitchWarning,
                    stacklevel=2,
                )
                stale_connection = connection
                connection = self._create_connection(connection_alias)
                storage[connection_alias] = connection
                self._schedule_stale_connection_close(stale_connection)
        except KeyError:
            connection = self._create_connection(connection_alias)
            storage[connection_alias] = connection
        return connection

    def _schedule_stale_connection_close(self, connection: DatabaseClient) -> None:
        """Closes a connection replaced because it was made on another event loop - on that loop while
        it still runs (another thread's), where its sockets are served, else as a task on the running
        loop; a failure is logged.
        """
        bound_loop = connection._bound_loop
        if bound_loop is not None and bound_loop.is_closed():
            # Its loop is gone - what it opened there is aborted, the rest closed below.
            try:
                connection.abandon_connections()
            except Exception as error:  # logged like a failed close
                logger.warning("Failed to close stale Hare connection after an event loop change: %s", error)
        elif bound_loop is not None and bound_loop.is_running():
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if bound_loop is not running_loop:
                future = asyncio.run_coroutine_threadsafe(connection.close(), bound_loop)
                self.stale_connection_closes.add(future)
                future.add_done_callback(self.stale_connection_closes.discard)
                future.add_done_callback(self._log_stale_connection_close_failure)
                return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(connection.close())
        self.stale_connection_closes.add(task)
        task.add_done_callback(self.stale_connection_closes.discard)
        task.add_done_callback(self._log_stale_connection_close_failure)

    @staticmethod
    def _log_stale_connection_close_failure(task: asyncio.Task[None] | concurrent.futures.Future[None]) -> None:
        if task.cancelled():
            return
        if (error := task.exception()) is not None:
            logger.warning("Failed to close stale Hare connection after an event loop change: %s", error)

    def get_single_client(self) -> DatabaseClient | None:
        """The client of the one configured connection.

        Returns:
            The client, None when several connections are configured.
        """
        if len(self.db_config) != 1:
            return None
        return self.get(next(iter(self.db_config)))

    def get_ambiguous_connection_error(self) -> QueryError:
        """The error of a call naming no connection while several are configured.

        Returns:
            The error, to raise.
        """
        return QueryError(
            f"You are running with multiple databases, so you should specify using: {list(self.db_config)}"
        )

    def create_independent(
        self,
        connection_alias: str,
        credential_overrides: Mapping[str, Any] | None = None,
        *,
        config: ConnectionConfig | None = None,
    ) -> DatabaseClient:
        """Builds a new connection to the database of ``connection_alias``, apart from the shared one - for
        ``Transactions.autonomous()``. The caller closes it.

        Args:
            connection_alias: The alias whose configuration is used.
            credential_overrides: Settings replacing the alias's own - a pool of one connection.
            config: Captured settings of a source client; when given, the alias is only its name.

        Raises:
            ConfigurationError: The alias doesn't exist.
        """
        connection = self._create_connection(connection_alias, credential_overrides, config=config)
        connection.pool_role = PoolRole.INDEPENDENT
        return connection

    def set(self, connection_alias: str, connection_client: DatabaseClient) -> ConnectionToken:
        """Sets ``connection_alias`` to ``connection_client`` for the current task - a transaction puts its client in
        place this way; ``reset()`` with the returned token restores the previous one.

        Args:
            connection_alias: The connection alias.
            connection_client: The connection.

        Returns:
            The token for ``reset()``.
        """
        frame = self.get_frame()
        token = ConnectionToken(
            _handler=self,
            _alias=connection_alias,
            _previous=frame.get_binding(connection_alias),
            _new_value=connection_client,
        )
        self.current_frame.set(frame.with_binding(connection_alias, token))
        return token

    def discard(self, connection_alias: str) -> DatabaseClient | None:
        """Drops the alias's connection from the current task context, without closing it - close it
        first, or it leaks.

        Args:
            connection_alias: The connection alias.

        Returns:
            The dropped connection, or None when the context held none.
        """
        self.current_frame.set(self.get_frame().without(connection_alias))
        return self.root_clients.pop(connection_alias, None)

    def reset(self, token: ConnectionToken | None) -> None:
        """Restores the connections of the current task context to what they were before ``set()``,
        keeping a connection created for another alias meanwhile.

        Args:
            token: The token ``set()`` returned; None does nothing.

        Raises:
            ValueError: The token was already used.
        """
        if token is None:
            return

        if token._used:
            raise ValueError("Token has already been used")
        token._used = True

        frame = self.get_frame()
        if frame.bindings.get(token._alias) is token:
            previous = frame.get_binding(token._alias)
            self.current_frame.set(
                frame.without(token._alias) if previous is None else frame.with_binding(token._alias, previous)
            )

    def get_non_transactional(self, connection_alias: str) -> DatabaseClient:
        """Returns the alias's shared, non-transactional client, even inside a transaction.

        Args:
            connection_alias: The connection alias.

        Raises:
            ConfigurationError: If the connection alias does not exist.
        """
        connection = self.get_main(connection_alias)
        if connection.tenant_schema_template is not None or connection.tenant_row_level_security:
            return connection.get_tenant_client()
        return connection

    def create_task_outside_transactions(self, coroutine: Coroutine[Any, Any, TaskResult]) -> asyncio.Task[TaskResult]:
        """Starts ``coroutine`` as a task that sees every alias's shared, non-transactional client
        - a background task started inside a transaction must not run its queries through it.

        Args:
            coroutine: The coroutine to run.

        Returns:
            The started task.
        """
        context = contextvars.copy_context()
        context.run(self._detach_from_transactions)
        return asyncio.create_task(coroutine, context=context)

    def _detach_from_transactions(self) -> None:
        self.current_frame.set(self.empty_frame)

    def all(self) -> list[DatabaseClient]:
        """The main connection of every configured alias, created where missing."""
        # Over db_config, not storage - a discarded alias is still configured.
        return [self.get_main(connection_alias) for connection_alias in self.db_config]

    def aliases(self) -> list[str]:
        """Returns every connection alias configured on this handler."""
        return list(self.db_config)

    def hand_over(self, successors: ConnectionHandler) -> None:
        """Hands each connection's queries to the one of the same alias another handler has - the
        connections of a repeated ``Hare.init()``: a query that took a client of this handler before it
        is closed runs on the new one.

        Args:
            successors: The handler replacing this one.
        """
        if self._db_config is None or successors._db_config is None:
            return
        for connection_alias, connection in tuple(self.root_clients.items()):
            if connection_alias in successors.db_config:
                connection.replaced_by = successors.get_main(connection_alias)

    async def close_all(self, discard: bool = True) -> None:
        """Closes the main connection of every configured alias.

        Args:
            discard: Whether to also drop the closed connections and mark them discarded; ``False`` keeps
                them, to open again on their next query.

        Raises:
            BaseException: A close error, or a group of errors, after every release finished.
        """
        if self.close_task is None:
            self.discard_on_close = discard
            self.close_task = asyncio.create_task(self._close_clients())
        elif discard:
            self.discard_on_close = True
        task = self.close_task
        try:
            await ResourceCompletion.wait(task)
        finally:
            if task.done() and self.close_task is task:
                self.close_task = None

    async def _close_clients(self) -> None:
        """Release resources of this handler; concurrent closers share this operation."""
        closers = tuple(self.resource_closers.values())
        resource_results = await asyncio.gather(*(close() for close in closers), return_exceptions=True)
        connections = list(self.root_clients.values())
        if self.discard_on_close:
            for connection in connections:
                connection.discarded = True
        tasks: list[Awaitable[None]] = [connection.close() for connection in connections]
        tasks.extend(
            asyncio.wrap_future(task) if isinstance(task, concurrent.futures.Future) else task
            for task in tuple(self.stale_connection_closes)
        )
        # Every connection is closed and dropped even when one fails.
        try:
            results = await ResourceCompletion.wait(asyncio.gather(*tasks, return_exceptions=True))
        finally:
            if self.discard_on_close:
                for connection in connections:
                    connection.discarded = True
                self.root_clients.clear()
            self.application = object()
            self.empty_frame = ConnectionFrame(self.application)
            self.current_frame.set(self.empty_frame)
            self.resource_closers.clear()
        errors = [result for result in (*resource_results, *results) if isinstance(result, BaseException)]
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup(CONNECTION_RELEASE_FAILED, errors)
