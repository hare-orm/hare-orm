"""asyncpg's reads through ``rust.native.rows.FieldCodec`` give what the field's own Python method gives - the
grid of reads of the core codec tests on the column types asyncpg returns natively. Its writes are rust_pg's:
both write by the PostgreSQL types."""

from __future__ import annotations

import pytest

from hare.dialects.postgresql.drivers.asyncpg.client.asyncpg_client import AsyncpgClient
from tests.native_acceleration.test_field_codecs import (
    BUILTIN_FIELD_CODECS,
    POSTGRESQL_TYPES,
    READ_CASES,
    assert_builtin_codecs,
    assert_reads_match,
)

REQUIRES = ("asyncpg", "native")

pytestmark = pytest.mark.database_independent

ASYNCPG_CONNECTIONS = [("asyncpg", POSTGRESQL_TYPES, AsyncpgClient.native_python_types)]


@pytest.mark.parametrize(
    ("model", "field_name", "values"),
    [pytest.param(*case, id=f"{case[0].__name__}.{case[1]}") for case in READ_CASES],
)
def test_reads_match_on_asyncpg(model, field_name, values):
    assert_reads_match(model, field_name, values, ASYNCPG_CONNECTIONS)


@pytest.mark.parametrize(("model", "field_name"), list(BUILTIN_FIELD_CODECS))
def test_builtin_fields_read_and_write_through_their_codecs_on_asyncpg(model, field_name):
    assert_builtin_codecs(model, field_name, BUILTIN_FIELD_CODECS[(model, field_name)], ASYNCPG_CONNECTIONS)
