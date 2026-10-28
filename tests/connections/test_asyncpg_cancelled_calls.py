"""A call asyncpg can't cancel - executemany() waits forever for the rest of its rows once cancelled - ends
its connection instead, the caller cancelled at once and the server rolling back."""

import asyncio

import pytest

from hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_client import AsyncpgClient  # noqa: E402

REQUIRES = ("asyncpg",)

pytestmark = pytest.mark.database_independent


class EndedConnection:
    def __init__(self) -> None:
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True


@pytest.mark.asyncio
async def test_a_cancelled_call_ends_its_connection_and_is_left():
    connection = EndedConnection()
    never_done = asyncio.get_running_loop().create_future()
    caller = asyncio.create_task(AsyncpgClient.run_ending_connection_on_cancel(connection, never_done))
    await asyncio.sleep(0)

    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert connection.terminated
    assert never_done.cancelled()


@pytest.mark.asyncio
async def test_a_call_ending_by_itself_gives_its_result_or_error():
    connection = EndedConnection()

    async def answer():
        return 5

    async def fail():
        raise ValueError("refused")

    assert await AsyncpgClient.run_ending_connection_on_cancel(connection, answer()) == 5
    with pytest.raises(ValueError, match="refused"):
        await AsyncpgClient.run_ending_connection_on_cancel(connection, fail())
    assert not connection.terminated
