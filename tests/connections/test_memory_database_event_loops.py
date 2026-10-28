"""An in-memory SQLite database lives in its one connection - a query from another event loop is refused
instead of opening a new, empty database and closing the one holding the data."""

import asyncio
import contextvars
import threading

import pytest

from hare.contrib.test.isolated_contexts import hare_test_context
from hare.exceptions import ConfigurationError
from tests.contrib.frameworks.models import Writer

pytestmark = pytest.mark.sqlite_only


@pytest.mark.asyncio
async def test_a_memory_database_is_refused_on_another_event_loop():
    async with hare_test_context(["tests.contrib.frameworks.models"], db_url="sqlite+aiosqlite://:memory:"):
        await Writer.objects.create(id=1, name="anna")
        errors: list[BaseException] = []

        async def count() -> int:
            return await Writer.objects.count()

        def count_on_a_loop_of_its_own() -> None:
            try:
                asyncio.run(count())
            except BaseException as error:
                errors.append(error)

        context = contextvars.copy_context()
        thread = threading.Thread(target=context.run, args=(count_on_a_loop_of_its_own,))
        thread.start()
        thread.join()

        assert [type(error) for error in errors] == [ConfigurationError]
        assert "lives in that connection only" in str(errors[0])
        assert await Writer.objects.count() == 1
