from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import AsyncGenerator
from typing import Any

from clickhouse_driver import Client

from hare.dialects.clickhouse.client.clickhouse_streamed_row import ClickhouseStreamedRow
from hare.dialects.clickhouse.drivers.clickhouse_driver.client.clickhouse_driver_connections import (
    ClickhouseDriverConnections,
)
from hare.dialects.clickhouse.drivers.clickhouse_driver.constants import (
    CLICKHOUSE_DRIVER_STREAM_QUEUE_SIZE,
    CLICKHOUSE_DRIVER_STREAM_WAIT_SECONDS,
)


class ClickhouseDriverRowStream:
    """The rows of one query read on a worker thread of the connections as the server sends them, and
    handed over a batch at a time - the thread waits while the reader holds the batches it hasn't read
    yet. A reader that stops early has the thread drop its connection: the server stops sending.

    Args:
        connections: The connections the query runs on.
        sql: The query.
        batch_size: How many rows a batch holds.
    """

    __slots__ = ("connections", "sql", "batch_size", "stopped")

    #: What the thread puts after the last batch.
    END: object = object()

    def __init__(self, connections: ClickhouseDriverConnections, sql: str, batch_size: int) -> None:
        self.connections = connections
        self.sql = sql
        self.batch_size = batch_size
        #: Set once the reader stops reading.
        self.stopped = threading.Event()

    async def read_batches(self) -> AsyncGenerator[list[ClickhouseStreamedRow]]:
        """The rows, a batch at a time.

        Yields:
            The rows of a batch, by position and by column name.

        Raises:
            Exception: The driver's error the query failed with.
        """
        loop = asyncio.get_running_loop()
        batches: asyncio.Queue[Any] = asyncio.Queue(maxsize=CLICKHOUSE_DRIVER_STREAM_QUEUE_SIZE)
        reading = self.connections.run(self.read_on_thread, loop, batches)
        try:
            while True:
                item = await batches.get()
                if item is self.END:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            self.stopped.set()
            while not batches.empty():
                batches.get_nowait()
            await reading

    def read_on_thread(
        self, library_client: Client, loop: asyncio.AbstractEventLoop, batches: asyncio.Queue[Any]
    ) -> None:
        """Reads the rows on the worker thread holding a connection, putting them in batches.

        Args:
            library_client: The thread's connection.
            loop: The reader's event loop.
            batches: The batches the reader takes.
        """
        reads_to_end = False
        try:
            rows = library_client.execute_iter(
                self.sql, settings={"max_block_size": self.batch_size}, with_column_types=True
            )
            row_class = ClickhouseStreamedRow.get_row_class(tuple(column[0] for column in next(rows)))
            batch: list[ClickhouseStreamedRow] = []
            for row in rows:
                batch.append(row_class(row))
                if len(batch) >= self.batch_size:
                    if not self.put(loop, batches, batch):
                        return
                    batch = []
            reads_to_end = True
            if batch and not self.put(loop, batches, batch):
                return
            self.put(loop, batches, self.END)
        except Exception as error:
            reads_to_end = True
            self.put(loop, batches, error)
        finally:
            if not reads_to_end:
                # The server is still sending the rows - the connection is dropped, the next statement
                # of the thread opens another.
                library_client.disconnect()

    def put(self, loop: asyncio.AbstractEventLoop, batches: asyncio.Queue[Any], item: Any) -> bool:
        """Hands a batch to the reader, waiting while it holds as many as it may.

        Args:
            loop: The reader's event loop.
            batches: The batches the reader takes.
            item: The batch, the end, or an error.

        Returns:
            Whether the reader took it - False once it stopped reading.
        """
        if self.stopped.is_set():
            return False
        putting = asyncio.run_coroutine_threadsafe(batches.put(item), loop)
        while True:
            try:
                putting.result(timeout=CLICKHOUSE_DRIVER_STREAM_WAIT_SECONDS)
                return True
            except concurrent.futures.TimeoutError:
                if self.stopped.is_set():
                    putting.cancel()
                    return False
