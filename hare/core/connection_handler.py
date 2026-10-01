from __future__ import annotations

import asyncio
import contextvars
import warnings
from collections.abc import Coroutine, Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from hare.core.log import logger
from hare.dialects.base.db_url import DbUrlConfigGenerator
from hare.dialects.registry import DialectRegistry
from hare.exceptions import ConfigurationError
from hare.warnings import HareLoopSwitchWarning

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient

    DBConfigType = dict[str, Any]
from hare.core.connection_token import ConnectionToken

TaskResult = TypeVar("TaskResult")


class ConnectionHandler:
    """
    Connection management for a single HareContext.

    Each HareContext owns its own ConnectionHandler instance with isolated storage.
    """

    def __init__(self) -> None:
        self._db_config: DBConfigType | None = None
        self._create_db: bool = False
        # Use ContextVar for task isolation within this handler instance.
        # This ensures transactions (which use .set()) are isolated to the task.
        self._storage_var: contextvars.ContextVar[dict[str, DatabaseClient]] = contextvars.ContextVar(
            f"storage_{id(self)}", default={}
        )

    @property
    def _storage(self) -> dict[str, DatabaseClient]:
        """
        Internal storage for connections.
        We use a property to provide a dict-like interface while being backed by a ContextVar.
        """
        return self._get_storage()

    @_storage.setter
    def _storage(self, value: dict[str, DatabaseClient]) -> None:
        """Allow direct assignment to storage for legacy compatibility (and tests)."""
        self._storage_var.set(value)

    def _get_storage(self) -> dict[str, DatabaseClient]:
        """Get the connection storage dict for the current task context."""
        return self._storage_var.get()

    def _set_storage(self, new_storage: dict[str, DatabaseClient]) -> None:
        """Set the connection storage dict. Used for testing purposes."""
        self._storage = new_storage

    def _copy_storage(self) -> dict[str, DatabaseClient]:
        """Return a shallow copy of the storage."""
        return dict(self._get_storage())

    def _clear_storage(self) -> None:
        """Clear all connections from storage in the current context."""
        self._storage_var.set({})

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
        """
        Return the DB config.

        This is the same config passed to the ``Hare.init()`` method while initialization.

        Raises:
            ConfigurationError: If this property is accessed before calling the
                ``Hare.init()`` method.
        """
        if self._db_config is None:
            raise ConfigurationError(
                "DB configuration not initialised. Make sure to call "
                "Hare.init with a valid configuration before attempting "
                "to create connections."
            )
        return self._db_config

    @staticmethod
    def _get_client_class(db_info: dict[str, Any]) -> type[DatabaseClient]:
        engine = db_info.get("engine")
        if not isinstance(engine, str) or not engine:
            raise ConfigurationError(f"Connection engine must be a non-empty driver name, got {engine!r}")
        return DialectRegistry.get_driver(engine).get_client_class(db_info["credentials"])

    def _get_db_info(self, conn_alias: str) -> str | dict[str, Any]:
        try:
            return self.db_config[conn_alias]
        except KeyError:
            raise ConfigurationError(
                f"Unable to get db settings for alias '{conn_alias}'. Please "
                f"check if the config dict contains this alias and try again"
            )

    async def _init_connections(self) -> None:
        try:
            for alias in self.db_config:
                connection: DatabaseClient = self.get(alias)
                if self._create_db:
                    await connection.db_create()
        except BaseException:
            # A failure on a later alias closes the connections already opened for earlier ones.
            try:
                await self.close_all(discard=True)
            except BaseException:
                # close_all() derives the broken alias again and may raise the same error - it
                # mustn't mask the original one.
                pass
            # Always reset: a retried init() merges into it and would carry the broken alias
            # forward.
            self._db_config = None
            raise

    def _create_connection(self, conn_alias: str) -> DatabaseClient:
        db_info = self._get_db_info(conn_alias)
        if isinstance(db_info, Mapping) and "db_url" in db_info:
            db_info = db_info["db_url"]
        if isinstance(db_info, str):
            db_info = DbUrlConfigGenerator.expand(db_info)
        if not isinstance(db_info, Mapping) or not isinstance(db_info.get("credentials", {}), Mapping):
            raise ConfigurationError(f"Invalid connection settings for alias '{conn_alias}'")
        db_info = {**db_info, "credentials": dict(db_info.get("credentials", {}))}
        client_class = self._get_client_class(db_info)
        db_params = db_info["credentials"].copy()
        db_params.update({"connection_name": conn_alias})
        try:
            connection: DatabaseClient = client_class(**db_params)
        except TypeError as exc:
            raise ConfigurationError(f"Invalid connection parameters for alias '{conn_alias}': {exc}") from exc
        return connection

    def get(self, conn_alias: str) -> DatabaseClient:
        """Returns the connection of ``conn_alias``, creating it if needed. A connection bound to
        another event loop is replaced, with a ``HareLoopSwitchWarning``.

        Args:
            conn_alias: The connection alias.

        Raises:
            ConfigurationError: The alias doesn't exist.
        """
        storage = self._storage_var.get()
        try:
            connection = storage[conn_alias]
            if not connection._check_loop():
                warnings.warn(
                    f"Hare connection '{conn_alias}' was created on a different "
                    f"event loop and will be reconnected. If this is expected (e.g., "
                    f"in tests), use hare_test_context() or suppress with: "
                    f"warnings.filterwarnings('ignore', "
                    f"category=HareLoopSwitchWarning)",
                    HareLoopSwitchWarning,
                    stacklevel=2,
                )
                stale_connection = connection
                connection = self._create_connection(conn_alias)
                storage[conn_alias] = connection
                self._schedule_stale_connection_close(stale_connection)
            return connection
        except KeyError:
            connection = self._create_connection(conn_alias)
            storage[conn_alias] = connection
            return connection

    def _schedule_stale_connection_close(self, connection: DatabaseClient) -> None:
        """Closes a connection replaced because its event loop is gone, as a task on the running loop;
        a failure is logged.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(connection.close())
        task.add_done_callback(self._log_stale_connection_close_failure)

    @staticmethod
    def _log_stale_connection_close_failure(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        if (exc := task.exception()) is not None:
            logger.warning("Failed to close stale Hare connection after an event loop change: %s", exc)

    def create_independent(self, conn_alias: str) -> DatabaseClient:
        """Builds a new connection to the database of ``conn_alias``, apart from the shared one - for
        ``Transactions.autonomous()``. The caller closes it.

        Args:
            conn_alias: The alias whose configuration is used.

        Raises:
            ConfigurationError: The alias doesn't exist.
        """
        return self._create_connection(conn_alias)

    def set(self, conn_alias: str, conn_obj: DatabaseClient) -> ConnectionToken:
        """Sets ``conn_alias`` to ``conn_obj`` for the current task - a transaction puts its client in
        place this way; ``reset()`` with the returned token restores the previous one.

        Args:
            conn_alias: The connection alias.
            conn_obj: The connection.

        Returns:
            The token for ``reset()``.
        """
        storage = self._storage_var.get()
        old_value = storage.get(conn_alias)
        storage_copy = dict(storage)
        storage_copy[conn_alias] = conn_obj
        cv_token = self._storage_var.set(storage_copy)
        return ConnectionToken(
            _handler=self, _alias=conn_alias, _old_value=old_value, _cv_token=cv_token, _new_value=conn_obj
        )

    def discard(self, conn_alias: str) -> DatabaseClient | None:
        """
        Discards the given alias from the storage in the `current context`.

        Make sure to have called ``connection.close()`` for the provided alias before calling this
        method else there would be a connection leak (dangling connection).

        Args:
            conn_alias: The alias for which the connection object should be discarded.
        """
        return self._get_storage().pop(conn_alias, None)

    def reset(self, token: ConnectionToken | None) -> None:
        """
        Reset the connection storage to the previous context state.

        Restores the connection state for all aliases to what it was before the set() call.

        Args:
            token: The token returned by the set() method. Can be None (no-op).
        """
        if token is None:
            return

        if token._used:
            raise ValueError("Token has already been used")
        token._used = True

        if token._cv_token and isinstance(token._cv_token, contextvars.Token):
            # set() installed a copy of the storage, and get() creates a connection into whichever
            # dict is current - a connection created for another alias during the transaction is
            # carried back.
            transaction_scoped_storage = self._storage_var.get()
            try:
                self._storage_var.reset(token._cv_token)
            except ValueError:
                # Reset from a task other than the one that called set() (a transaction context
                # exited in another task) - that task's context never saw this token.
                self._restore_alias_outside_token_context(token)
                return
            restored_storage = self._storage_var.get()
            for alias, connection in transaction_scoped_storage.items():
                if alias == token._alias:
                    # The alias's own entry is what reset() restores.
                    continue
                if alias not in restored_storage:
                    restored_storage[alias] = connection
                elif restored_storage[alias] is not connection:
                    # Another task created a client for the same alias meanwhile - this one loses
                    # the slot and is closed, not leaked.
                    self._schedule_stale_connection_close(connection)
        else:
            # Fallback when no ContextVar token (e.g., mock tokens in tests)
            storage = self._copy_storage()
            if token._old_value is None:
                storage.pop(token._alias, None)
            else:
                storage[token._alias] = token._old_value
            self._storage = storage

    def _restore_alias_outside_token_context(self, token: ConnectionToken) -> None:
        """Puts the alias's pre-set() client back in the current context when it still holds the
        client set() installed - the fallback for a token reset from another context.

        Args:
            token: The token returned by the set() method.
        """
        storage = self._get_storage()
        if token._alias not in storage or storage[token._alias] is not token._new_value:
            return
        restored_storage = self._copy_storage()
        if token._old_value is None:
            restored_storage.pop(token._alias, None)
        else:
            restored_storage[token._alias] = token._old_value
        self._storage_var.set(restored_storage)

    def get_non_transactional(self, conn_alias: str) -> DatabaseClient:
        """Returns the alias's shared, non-transactional client, even inside a transaction.

        Args:
            conn_alias: The connection alias.

        Raises:
            ConfigurationError: If the connection alias does not exist.
        """
        return self._get_non_transactional_client(self.get(conn_alias))

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
        storage = self._get_storage()
        detached_storage = {
            alias: self._get_non_transactional_client(connection) for alias, connection in storage.items()
        }
        if any(detached_storage[alias] is not connection for alias, connection in storage.items()):
            self._storage_var.set(detached_storage)

    @staticmethod
    def _get_non_transactional_client(connection: DatabaseClient) -> DatabaseClient:
        from hare.dialects.base.client.transaction_client import TransactionClient

        if isinstance(connection, TransactionClient):
            return connection.get_non_transactional_client()
        return connection

    def all(self) -> list[DatabaseClient]:
        """Returns a list of connection objects from the storage in the `current context`."""
        # Over db_config, not storage - a discarded alias is still configured.
        return [self.get(alias) for alias in self.db_config]

    def aliases(self) -> list[str]:
        """Returns every connection alias configured on this handler."""
        return list(self.db_config)

    async def close_all(self, discard: bool = True) -> None:
        """
        Closes all connections in the storage in the `current context`.

        All closed connections will be removed from the storage by default.

        Args:
            discard: If ``False``, all connection objects are closed but `retained` in the storage.
        """
        # Handle case where connections were never initialized (e.g., init failed)
        if self._db_config is None:
            return
        tasks = [connection.close() for connection in self.all()]
        # Every connection is closed and dropped even when one fails; the first failure is raised
        # afterwards.
        results = await asyncio.gather(*tasks, return_exceptions=True)
        if discard:
            for alias in self.db_config:
                self.discard(alias)
        for result in results:
            if isinstance(result, BaseException):
                raise result
