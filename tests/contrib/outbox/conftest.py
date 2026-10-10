"""Custom fixture for tests/contrib/outbox/ - uses tests.contrib.outbox.models instead of the
default tests.testmodels, matching the convention in tests/dialects/postgresql/conftest.py and
tests/fields/conftest.py.
"""

import os

import pytest_asyncio

from hare.contrib.test import truncate_all_models
from hare.contrib.test.isolated_contexts import hare_test_context


@pytest_asyncio.fixture(scope="module")
async def db_outbox_module():
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        modules=["tests.contrib.outbox.models"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
    ) as ctx:
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
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        modules=["tests.contrib.outbox.capture_models"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture(scope="function")
async def db_capture(db_capture_module):
    """Truncate-cleanup: the tests check what commits and what rolls back."""
    yield db_capture_module
    await truncate_all_models()
