from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_library_client import (
    ClickhouseDriverLibraryClient,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.constants import (
    CLICKHOUSE_DRIVER_CLOSE_TIMEOUT_SECONDS,
    CLICKHOUSE_DRIVER_CLOSED_MESSAGE,
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
        "lock",
    )

    def __init__(self, max_size: int, connection_settings: dict[str, Any], statistics: Any) -> None:
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
        self.statistics.acquire_count += 1
        submit_time = time.perf_counter() if PoolMetrics.enabled else 0.0
        statement = self.executor.submit(self._run_on_thread, function, arguments, submit_time)
        self.running_statements.add(statement)
        statement.add_done_callback(self.running_statements.discard)
        return asyncio.wrap_future(statement)

    def _run_on_thread(
        self, function: Callable[..., ResultType], arguments: tuple[Any, ...], submit_time: float
    ) -> ResultType:
        library_client: ClickhouseDriverLibraryClient | None = getattr(self.thread_connections, "library_client", None)
        is_new = library_client is None
        if library_client is None:
            library_client = ClickhouseDriverLibraryClient(**self.connection_settings)
            self.thread_connections.library_client = library_client
        with self.lock:
            if self.is_closed:
                raise DBConnectionError(CLICKHOUSE_DRIVER_CLOSED_MESSAGE)
            if submit_time:
                self.statistics.add_wait(time.perf_counter() - submit_time)
            if is_new:
                self.library_clients.append(library_client)
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
            connection.disconnect()
            with self.lock:
                self.statistics.add_connect_failure()
            raise
        # Just opened - its first statement needs no ping.
        library_client.last_statement_time = time.monotonic()
        with self.lock:
            self.statistics.add_connect(time.perf_counter() - start_time)

    def get_occupancy(self) -> tuple[int, int, int, int, int]:
        """How the connections are taken.

        Returns:
            The open, the idle and the waited-for connections, the least and the most of them.
        """
        open_count = sum(map(self._is_connected, self.library_clients))
        running_count = len(self.running_statements)
        busy_count = min(running_count, self.max_size)
        return open_count, max(0, open_count - busy_count), running_count - busy_count, 0, self.max_size

    async def close(self) -> None:
        """Ends the worker threads and closes their connections. The statements handed over
        already are waited for ``CLICKHOUSE_DRIVER_CLOSE_TIMEOUT_SECONDS``; after that the ones not
        started fail, and a statement still running ends on its own - its thread closes its
        connection then.
        """
        self.executor.shutdown(wait=False)
        running_statements = list(self.running_statements)
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
            self.library_clients.clear()
        for library_client in idle_library_clients:
            self._disconnect(library_client)

    @staticmethod
    def _disconnect(library_client: ClickhouseDriverLibraryClient) -> None:
        # A library client has no connection until its first statement picks one.
        if getattr(library_client, "connection", None) is not None:
            library_client.disconnect()
