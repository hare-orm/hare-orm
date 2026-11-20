"""The database of tests/contrib/taskiq/ - its own models, emptied after each test: the tests check
real commits and rollbacks, which a wrapping rollback transaction would hide."""

import pytest_asyncio

from hare.contrib.test import truncate_all_models
from tests.utils.database_under_test import DatabaseUnderTest


@pytest_asyncio.fixture(scope="module")
async def db_taskiq_module():
    async with DatabaseUnderTest.open_model_context(["tests.contrib.taskiq.models"]) as context:
        yield context


@pytest_asyncio.fixture(scope="function")
async def db_taskiq(db_taskiq_module):
    yield db_taskiq_module
    await truncate_all_models()
