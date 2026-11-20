"""The database of the write rules tests - their own models, created once per test module, each test rolled
back - and the encryption key their encrypted fields need."""

from collections.abc import AsyncGenerator, Generator
from typing import Any

import pytest
import pytest_asyncio

from hare.contrib.test import RollbackIsolation
from hare.fields.encrypted.field_encryption import FieldEncryption
from tests.utils.database_under_test import DatabaseUnderTest

#: The secret the encrypted fields of the write rules models are encrypted and indexed with.
WRITE_RULES_SECRET = "write-rules-test-secret"


@pytest.fixture(autouse=True)
def configured_keys() -> Generator[None]:
    FieldEncryption.configure(WRITE_RULES_SECRET)
    yield
    FieldEncryption.reset()


@pytest_asyncio.fixture(scope="module")
async def rules_context() -> AsyncGenerator[Any]:
    async with DatabaseUnderTest.open_model_context(["tests.writes.rules.write_rules_models"]) as context:
        yield context


@pytest_asyncio.fixture
async def rules_db(rules_context: Any) -> AsyncGenerator[Any]:
    async with RollbackIsolation(rules_context) as context:
        yield context
