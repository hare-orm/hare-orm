"""Custom fixture for tests/contrib/outbox/ - uses tests.contrib.outbox.models instead of the
default tests.shared_models.testmodels, matching the convention in tests/dialects/postgresql/conftest.py and
tests/fields/conftest.py.
"""

import pytest_asyncio

from hare.contrib.test import truncate_all_models
from tests.utils.database_under_test import DatabaseUnderTest


@pytest_asyncio.fixture(scope="module")
async def db_outbox_module():
    async with DatabaseUnderTest.open_model_context(["tests.contrib.outbox.models"]) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_outbox(db_outbox_module):
    """Truncate-cleanup, not transaction-rollback: several tests here (test_outbox_atomicity.py)
    assert on real commit/rollback/"persists outside any transaction" semantics, which a
    wrapping rollback transaction would either mask or falsify."""
    yield db_outbox_module
    await truncate_all_models()


@pytest_asyncio.fixture(scope="module")
async def db_capture_module():
    async with DatabaseUnderTest.open_model_context(["tests.contrib.outbox.capture_models"]) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_capture(db_capture_module):
    """Truncate-cleanup: the tests check what commits and what rolls back."""
    yield db_capture_module
    await truncate_all_models()
