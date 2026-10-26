"""The database of a contract test - a database with no table: the test's migrations create the
tables of its samples, on every dialect."""

import os
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio

from hare.contrib.test.isolated_contexts import hare_test_context
from hare.core.hare_context import HareContext
from hare.fields.encrypted.field_encryption import FieldEncryption
from tests.contracts.constants import CONTRACT_CONNECTION
from tests.matrix.constants import REQUIRES_MARKER

#: The key the encrypted samples are encrypted with.
CONTRACT_ENCRYPTION_KEY = "contract-tests-secret"


@pytest.fixture(autouse=True)
def encryption_key(request: pytest.FixtureRequest):
    """The encryption key of a test requiring the encryption extra - configuring it needs cryptography, which
    only the environments of the extra install."""
    required = {name for marker in request.node.iter_markers(REQUIRES_MARKER) for name in marker.args}
    if "encryption" not in required:
        yield
        return
    FieldEncryption.configure(CONTRACT_ENCRYPTION_KEY)
    yield
    FieldEncryption.reset()


@pytest_asyncio.fixture
async def contract_database() -> AsyncGenerator[HareContext]:
    async with hare_test_context(
        modules=["tests.contracts.models_anchor"],
        db_url=os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:"),
        app_label="contract_anchor",
        connection_label=CONTRACT_CONNECTION,
        _generate_schemas=False,
    ) as context:
        yield context
