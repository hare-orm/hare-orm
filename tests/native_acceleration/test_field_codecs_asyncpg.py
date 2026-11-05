"""Model hydration respects field conversions on asyncpg's native column types."""

from __future__ import annotations

import pytest

from hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_client import AsyncpgClient
from tests.native_acceleration.test_field_codecs import (
    POSTGRESQL_TYPES,
    READ_CASES,
    assert_reads_match,
)

pytestmark = pytest.mark.database_independent

ASYNCPG_CONNECTIONS = [("asyncpg", POSTGRESQL_TYPES, AsyncpgClient.native_python_types)]


@pytest.mark.parametrize(
    ("model", "field_name", "values"),
    [pytest.param(*case, id=f"{case[0].__name__}.{case[1]}") for case in READ_CASES],
)
def test_reads_match_on_asyncpg(model, field_name, values):
    assert_reads_match(model, field_name, values, ASYNCPG_CONNECTIONS)
