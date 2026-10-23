"""The database of tests/contrib/taskiq/ - its own models, emptied after each test: the tests check
real commits and rollbacks, which a wrapping rollback transaction would hide."""

import os

import pytest
import pytest_asyncio

from hare.contrib.test import truncate_all_models
from hare.contrib.test.isolated_contexts import hare_test_context

pytest.importorskip("taskiq")


@pytest_asyncio.fixture(scope="module")
async def db_taskiq_module():
    async with hare_test_context(
        modules=["tests.contrib.taskiq.models"],
        db_url=os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:"),
        app_label="models",
        connection_label="models",
    ) as context:
        yield context


@pytest_asyncio.fixture(scope="function")
async def db_taskiq(db_taskiq_module):
    yield db_taskiq_module
    await truncate_all_models()
