from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

from hare.core.connections.messages import CONNECTION_RELEASE_FAILED
from hare.core.resource_completion import ResourceCompletion
from hare.dialects.base.client.driver_statement_log_filter import DriverStatementLogFilter
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_library_client import (
    ClickhouseDriverLibraryClient,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.constants import (
    CLICKHOUSE_DRIVER_CLOSE_TIMEOUT_SECONDS,
    CLICKHOUSE_DRIVER_CLOSED_MESSAGE,
    CLICKHOUSE_DRIVER_LIBRARY_LOGGER_NAME,
    CLICKHOUSE_DRIVER_STATEMENT_LOG_FORMATS,
    CLICKHOUSE_DRIVER_THREAD_NAME_PREFIX,
)
from hare.exceptions import DBConnectionError
from hare.instrumentation.pools.pool_metrics import PoolMetrics

ResultType = TypeVar("ResultType")


class ClickhouseDriverConnections:
    """The connections of a clickhouse-driver client. The library is synchronous and a connection
    runs one statement at a time, so each worker thread keeps a connection of its own, opened by
    its first statement: ``max_size`` threads run that many statements at once, the others wait
    their turn.

    Args:
        max_size: The most worker threads - the most connections.
        connection_settings: The arguments of the library's ``Client``.
        statistics: The pool statistics of the client.
    """

    __slots__ = (
        "max_size",
        "connection_settings",
        "statistics",
        "executor",
        "thread_connections",
        "library_clients",
        "busy_library_clients",
        "running_statements",
        "is_closed",
        "accepting",
        "closing",
        "lock",
    )

    def __init__(self, max_size: int, connection_settings: dict[str, Any], statistics: Any) -> None:
        DriverStatementLogFilter.install(
            CLICKHOUSE_DRIVER_LIBRARY_LOGGER_NAME, CLICKHOUSE_DRIVER_STATEMENT_LOG_FORMATS
        )
        self.max_size = max_size
        self.connection_settings = connection_settings
        self.statistics = statistics
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_size, thread_name_prefix=CLICKHOUSE_DRIVER_THREAD_NAME_PREFIX
        )
        #: The library client of each worker thread, read by the thread alone.
        self.thread_connections = threading.local()
        self.library_clients: list[ClickhouseDriverLibraryClient] = []
        self.busy_library_clients: set[ClickhouseDriverLibraryClient] = set()
        #: The statements handed to the worker threads and not finished yet.
        self.running_statements: set[concurrent.futures.Future[Any]] = set()
        #: Set once close() stopped waiting - a statement not started by then fails.
        self.is_closed = False
        self.accepting = True
        self.closing: asyncio.Task[None] | None = None
        #: Held where a worker thread changes what the others change too.
        self.lock = threading.Lock()

    def run(self, function: Callable[..., ResultType], *arguments: Any) -> asyncio.Future[ResultType]:
        """Runs ``function(library_client, *arguments)`` on a worker thread, with the thread's own
        connection. Cancelling the wait drops a statement that hasn't started; a started one runs
        to its end - the server is not told to stop it.

        Args:
            function: What to run.
            arguments: Its arguments after the library client.

        Returns:
            The awaitable of its result.
        """
        return asyncio.wrap_future(self.submit(function, *arguments))

    def submit(self, function: Callable[..., ResultType], *arguments: Any) -> concurrent.futures.Future[ResultType]:
        """Admit wire IO and expose its actual completion independently of asyncio waiters.

        Future.cancel() prevents queued work from starting but cannot cancel running IO.
        Completion callbacks and pool accounting track this Future, not its asyncio wrapper.
        """
        submit_time = time.perf_counter() if PoolMetrics.enabled else 0.0
        with self.lock:
            if not self.accepting:
                raise DBConnectionError(CLICKHOUSE_DRIVER_CLOSED_MESSAGE)
            self.statistics.acquire_count += 1
            statement = self.executor.submit(self._run_on_thread, function, arguments, submit_time)
            self.running_statements.add(statement)
        # add_done_callback can call synchronously for an already completed Future.
        statement.add_done_callback(self._forget_statement)
        return statement

    def _forget_statement(self, statement: concurrent.futures.Future[Any]) -> None:
        """Forget completed wire IO, independently of cancellation of its asyncio waiter."""
        with self.lock:
            self.running_statements.discard(statement)

    def _run_on_thread(
        self, function: Callable[..., ResultType], arguments: tuple[Any, ...], submit_time: float
    ) -> ResultType:
        with self.lock:
            if self.is_closed:
                raise DBConnectionError(CLICKHOUSE_DRIVER_CLOSED_MESSAGE)
            library_client: ClickhouseDriverLibraryClient | None = getattr(
                self.thread_connections, "library_client", None
            )
            if library_client is None:
                library_client = ClickhouseDriverLibraryClient(**self.connection_settings)
                self.thread_connections.library_client = library_client
                self.library_clients.append(library_client)
            if submit_time:
                self.statistics.add_wait(time.perf_counter() - submit_time)
            # Taken before its connection opens: close() leaves a taken one to its thread.
            self.busy_library_clients.add(library_client)
        try:
            if not self._is_connected(library_client):
                self._connect(library_client)
            return function(library_client, *arguments)
        finally:
            self._release(library_client)

    def _release(self, library_client: ClickhouseDriverLibraryClient) -> None:
        """Gives back a thread's connection once its statement ended - closed here when close()
        ran meanwhile, which leaves a taken connection to its thread."""
        with self.lock:
            self.busy_library_clients.discard(library_client)
            is_closed = self.is_closed
            if is_closed:
                self.library_clients.remove(library_client)
        if is_closed:
            self._disconnect(library_client)

    @staticmethod
    def _is_connected(library_client: ClickhouseDriverLibraryClient) -> bool:
        # The library client has no connection until its first statement picks one.
        connection = getattr(library_client, "connection", None)
        return connection is not None and connection.connected

    def _connect(self, library_client: ClickhouseDriverLibraryClient) -> None:
        """Opens a library client's connection - the first one of its thread, or the one after a
        failed statement, which the library closes."""
        connection = getattr(library_client, "connection", None)
        if connection is None:
            connection = library_client.connection = library_client.get_connection()
        start_time = time.perf_counter()
        try:
            connection.connect()
        except BaseException:
            # A handshake the server refused leaves the socket open.
            try:
                connection.disconnect()
            finally:
                with self.lock:
                    self.statistics.add_connect_failure()
            raise
        with self.lock:
            self.statistics.add_connect(time.perf_counter() - start_time)

    def get_occupancy(self) -> tuple[int, int, int, int, int]:
        """How the connections are taken.

        Returns:
            The open, the idle and the waited-for connections, the least and the most of them.
        """
        with self.lock:
            open_count = sum(map(self._is_connected, self.library_clients))
            busy_count = len(self.busy_library_clients)
            waiting_count = sum(
                not statement.running() and not statement.done() for statement in self.running_statements
            )
        return open_count, max(0, open_count - busy_count), waiting_count, 0, self.max_size

    async def close(self) -> None:
        """Ends the worker threads and closes their connections. The statements handed over
        already are waited for ``CLICKHOUSE_DRIVER_CLOSE_TIMEOUT_SECONDS``; after that the ones not
        started fail, and a statement still running ends on its own - its thread closes its
        connection then.
        """
        if self.closing is None:
            with self.lock:
                self.accepting = False
            self.closing = asyncio.create_task(self._close())
        await ResourceCompletion.wait(self.closing)

    async def _close(self) -> None:
        """Drain admitted work for the grace period, then close the pool to queued work.

        concurrent Future state records submitted, running and completed IO. A cancelled
        asyncio waiter cannot return a running connection to the pool. Busy connections
        remain with their worker and disconnect on release after the grace period.
        Queued work reaches the closed-pool check when its worker is available; cancelling
        its Future here would turn the pool's connection error into caller cancellation.
        """
        self.executor.shutdown(wait=False)
        with self.lock:
            running_statements = tuple(self.running_statements)
        if running_statements:
            await asyncio.get_running_loop().run_in_executor(
                None, concurrent.futures.wait, running_statements, CLICKHOUSE_DRIVER_CLOSE_TIMEOUT_SECONDS
            )
        with self.lock:
            self.is_closed = True
            idle_library_clients = [
                library_client
                for library_client in self.library_clients
                if library_client not in self.busy_library_clients
            ]
            self.library_clients = [
                library_client
                for library_client in self.library_clients
                if library_client in self.busy_library_clients
            ]
        errors: list[BaseException] = []
        for library_client in idle_library_clients:
            try:
                self._disconnect(library_client)
            except BaseException as error:
                errors.append(error)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup(CONNECTION_RELEASE_FAILED, errors)

    @staticmethod
    def _disconnect(library_client: ClickhouseDriverLibraryClient) -> None:
        # A library client has no connection until its first statement picks one.
        if getattr(library_client, "connection", None) is not None:
            library_client.disconnect()
