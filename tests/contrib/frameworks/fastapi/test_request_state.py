"""FastAPI's integration starts a request with its own state: an empty count of repeated queries and none of
the writes made before it in its task - by an earlier request or at startup."""

import pytest

from hare.contrib.frameworks.fastapi.middleware.request_state_reset_middleware import RequestStateResetMiddleware
from tests.contrib.frameworks.request_state import in_a_task_that_wrote_before, receive, request_state, send

REQUIRES = ("fastapi",)

pytestmark = pytest.mark.database_independent


@pytest.mark.asyncio
async def test_fastapi_starts_a_request_with_its_own_state():
    async def app(scope, receive, send):
        app.state = request_state()

    middleware = RequestStateResetMiddleware(app)
    await in_a_task_that_wrote_before(lambda: middleware({"type": "http"}, receive, send))
    assert app.state == (None, {})
