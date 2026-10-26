"""A transaction of a driver whose connections belong to the event loop they were opened on (asyncpg) is
refused from another loop with what to do - the driver failed with its own "attached to a different loop"
and broke the connection for the transaction's own loop too."""

import asyncio
import contextvars
import threading

import pytest

from hare.contrib.test.isolated_contexts import hare_test_context
from hare.exceptions import ConfigurationError
from hare.transactions.transactions import Transactions
from tests.contrib.frameworks.models import Writer
from tests.utils.database_under_test import DatabaseUnderTest

pytestmark = pytest.mark.skipif(
    DatabaseUnderTest.get_dialect().name != "postgresql",
    reason="the PostgreSQL drivers - one bound to its loop, one not",
)


async def count_writers() -> int:
    return await Writer.objects.count()


def run_on_a_loop_of_its_own(coroutine_function) -> list[BaseException]:
    errors: list[BaseException] = []

    def run() -> None:
        try:
            asyncio.run(coroutine_function())
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=contextvars.copy_context().run, args=(run,))
    thread.start()
    thread.join()
    return errors


@pytest.mark.asyncio
async def test_a_transaction_runs_its_statements_on_its_loop_only_where_the_driver_needs_it():
    async with hare_test_context(["tests.contrib.frameworks.models"], db_url=DatabaseUnderTest.get_url()) as context:
        connection = context.get_connection()
        async with Transactions.atomic():
            await Writer.objects.create(id=1, name="anna")
            errors = run_on_a_loop_of_its_own(count_writers)
            if type(connection).connections_bound_to_event_loop:
                assert [type(error) for error in errors] == [ConfigurationError]
                assert "began on another event loop" in str(errors[0])
            else:
                assert errors == []
            assert await Writer.objects.count() == 1
