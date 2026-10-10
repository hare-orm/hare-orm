"""Every framework integration starts a request with its own state: an empty count of repeated
queries and none of the writes made before it in its task - by an earlier request or at startup."""

import asyncio
from typing import Any

import pytest

from hare.contrib.frameworks.fastapi.middleware import RequestStateResetMiddleware as FastAPIRequestStateReset
from hare.contrib.frameworks.litestar.middleware import RequestStateResetMiddleware as LitestarRequestStateReset
from hare.contrib.frameworks.robyn import HareRobyn
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


@pytest.mark.asyncio
async def test_fastapi_starts_a_request_with_its_own_state():
    async def app(scope, receive, send):
        app.state = request_state()

    middleware = FastAPIRequestStateReset(app)
    await in_a_task_that_wrote_before(lambda: middleware({"type": "http"}, receive, send))
    assert app.state == (None, {})


@pytest.mark.asyncio
async def test_litestar_starts_a_request_with_its_own_state():
    async def next_app(scope, receive, send):
        next_app.state = request_state()

    middleware = LitestarRequestStateReset()
    await in_a_task_that_wrote_before(lambda: middleware.handle({"type": "http"}, receive, send, next_app))
    assert next_app.state == (None, {})


@pytest.mark.asyncio
async def test_robyn_starts_a_request_with_its_own_state():
    async def start_request():
        request = object()
        assert await HareRobyn.reset_request_state(request) is request
        return request_state()

    assert await in_a_task_that_wrote_before(start_request) == (None, {})
