"""The database of the write rules tests - their own models, created once per test module, each test rolled
back - and the encryption key their encrypted fields need."""

import os
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any

import pytest
import pytest_asyncio

from hare.contrib.test import RollbackIsolation
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.fields.encrypted.field_encryption import FieldEncryption

#: The secret the encrypted fields of the write rules models are encrypted and indexed with.
WRITE_RULES_SECRET = "write-rules-test-secret"


def get_test_db_url() -> str:
    raw_db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:").replace("\\{", "{").replace("\\}", "}")
    return raw_db_url.format(uuid.uuid4().hex) if "{}" in raw_db_url else raw_db_url


@pytest.fixture(autouse=True)
def configured_keys() -> Generator[None]:
    FieldEncryption.configure(WRITE_RULES_SECRET)
    yield
    FieldEncryption.reset()


@pytest_asyncio.fixture(scope="module")
async def rules_context() -> AsyncGenerator[Any]:
    async with hare_test_context(
        ["tests.writes.rules.write_rules_models"], db_url=get_test_db_url(), connection_label="models"
    ) as context:
        yield context


@pytest_asyncio.fixture
async def rules_db(rules_context: Any) -> AsyncGenerator[Any]:
    async with RollbackIsolation(rules_context) as context:
        yield context
