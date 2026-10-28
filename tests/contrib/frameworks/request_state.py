"""The state a framework integration starts a request with - an empty count of repeated queries and none of
the writes made before it in its task, by an earlier request or at startup."""

import asyncio
from typing import Any

from hare.contrib.repeated_queries import RepeatedQueryDetector
from hare.core.routing.written_connections import WrittenConnections


def request_state() -> tuple[Any, Any]:
    return WrittenConnections.current.get(), RepeatedQueryDetector.current_counts.get()


async def receive() -> dict[str, Any]:
    return {"type": "http.request"}


async def send(message: dict[str, Any]) -> None:
    pass


async def in_a_task_that_wrote_before(start_request) -> tuple[Any, Any]:
    """The state a request's handler sees after ``start_request`` ran in a task with an earlier write
    and an earlier count of repeated queries."""

    async def task():
        WrittenConnections.current.set(WrittenConnections())
        RepeatedQueryDetector.current_counts.set({"earlier": {}})
        return await start_request()

    return await asyncio.create_task(task())
