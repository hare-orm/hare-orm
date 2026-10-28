"""Robyn's integration starts a request with its own state: an empty count of repeated queries and none of
the writes made before it in its task - by an earlier request or at startup."""

import pytest

from hare.contrib.frameworks.robyn import HareRobyn
from tests.contrib.frameworks.request_state import in_a_task_that_wrote_before, request_state

REQUIRES = ("robyn",)

pytestmark = pytest.mark.database_independent


@pytest.mark.asyncio
async def test_robyn_starts_a_request_with_its_own_state():
    async def start_request():
        request = object()
        assert HareRobyn.reset_request_state(request) is request
        return request_state()

    assert await in_a_task_that_wrote_before(start_request) == (None, {})
