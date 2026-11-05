from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import AsyncGenerator

from clickhouse_driver import Client

from hare.core.resource_completion import ResourceCompletion
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
        batches: asyncio.Queue[list[ClickhouseStreamedRow]] = asyncio.Queue(
            maxsize=CLICKHOUSE_DRIVER_STREAM_QUEUE_SIZE
        )
        submitted = self.connections.submit(self.read_on_thread, loop, batches)
        reading = asyncio.wrap_future(submitted)
        receiving: asyncio.Task[list[ClickhouseStreamedRow]] | None = None
        try:
            while True:
                if not batches.empty():
                    yield batches.get_nowait()
                    continue
                if reading.done():
                    reading.result()
                    return
                receiving = asyncio.create_task(batches.get())
                await asyncio.wait((receiving, reading), return_when=asyncio.FIRST_COMPLETED)
                if receiving.done():
                    yield receiving.result()
                    receiving = None
                else:
                    receiving.cancel()
                    await asyncio.gather(receiving, return_exceptions=True)
                    receiving = None
                    # A connection/producer failure may occur before the first batch.
                    # Completion is carried by the Future, never a queue sentinel.
                    reading.result()
        finally:
            self.stopped.set()
            cancelled_before_start = submitted.cancel()
            await ResourceCompletion.wait(self.finish_reading(reading, receiving, batches, cancelled_before_start))

    @staticmethod
    async def finish_reading(
        reading: asyncio.Future[None],
        receiving: asyncio.Task[list[ClickhouseStreamedRow]] | None,
        batches: asyncio.Queue[list[ClickhouseStreamedRow]],
        cancelled_before_start: bool,
    ) -> None:
        """Release the bounded queue and wait until the worker releases its connection."""
        if receiving is not None:
            receiving.cancel()
            await asyncio.gather(receiving, return_exceptions=True)
        while not batches.empty():
            batches.get_nowait()
        if cancelled_before_start:
            await asyncio.gather(reading, return_exceptions=True)
        else:
            await reading

    def read_on_thread(
        self,
        library_client: Client,
        loop: asyncio.AbstractEventLoop,
        batches: asyncio.Queue[list[ClickhouseStreamedRow]],
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
        finally:
            if not reads_to_end:
                # The server is still sending the rows - the connection is dropped, the next statement
                # of the thread opens another.
                library_client.disconnect()

    def put(
        self,
        loop: asyncio.AbstractEventLoop,
        batches: asyncio.Queue[list[ClickhouseStreamedRow]],
        item: list[ClickhouseStreamedRow],
    ) -> bool:
        """Hands a batch to the reader, waiting while it holds as many as it may.

        Args:
            loop: The reader's event loop.
            batches: The batches the reader takes.
            item: The batch of rows. Completion and errors belong to the producer's Future.

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
