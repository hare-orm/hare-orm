from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast

from hare.contrib.test.queries.query_counter import QueryCounter
from hare.core.hare_context import HareContext
from hare.instrumentation.declarations import QueryExecuted
from hare.instrumentation.observers.observers import Observers

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient


@asynccontextmanager
async def capture_queries(using: str | DatabaseClient | None = None) -> AsyncGenerator[QueryCounter]:
    """Counts every query hare sends to ``using`` from the block and the tasks it starts - every
    ``QueryExecuted`` of the connection, COPY included.

    Args:
        using: A connection alias or client - the current context's default connection by default.

    Example::

        async with capture_queries() as counter:
            await Event.objects.all().select_related("tournament")
        assert counter.count == 1
    """
    connection = cast(
        "DatabaseClient", using if hasattr(using, "execute") else HareContext.require_current().get_connection(using)
    )
    counter = QueryCounter(connection.connection_alias)
    with Observers.observing(QueryExecuted, counter.record):
        yield counter


@asynccontextmanager
async def assert_query_count(
    expected: int, *, using: str | DatabaseClient | None = None
) -> AsyncGenerator[QueryCounter]:
    """Asserts the block runs exactly ``expected`` queries against ``using`` - an N+1 regression fails
    the test. The failure lists every captured query's SQL.

    Args:
        expected: The number of queries.
        using: A connection alias or client - the current context's default connection by default.

    Example::

        async with assert_query_count(1):
            await Event.objects.all().select_related("tournament")
    """
    async with capture_queries(using) as counter:
        yield counter
    if counter.count != expected:
        listed = "\n".join(f"  {number + 1}. {query}" for number, query in enumerate(counter.queries)) or "  (none)"
        plural = "y" if expected == 1 else "ies"
        raise AssertionError(f"Expected {expected} quer{plural}, executed {counter.count}:\n{listed}")
