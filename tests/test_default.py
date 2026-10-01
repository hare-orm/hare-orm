import datetime
from decimal import Decimal

import pytest
import pytest_asyncio

from hare import Connections
from hare.dialects.postgresql.client import PostgresqlClient
from hare.dialects.sqlite.client import SqliteClient
from hare.utils import UTC
from tests.testmodels import DefaultModel


@pytest_asyncio.fixture
async def default_row(db):
    """Insert a default row using raw SQL based on database type."""
    db_conn = Connections.get("models")
    if isinstance(db_conn, SqliteClient):
        await db_conn.execute(
            "insert into defaultmodel default values",
        )
    elif isinstance(db_conn, PostgresqlClient):
        await db_conn.execute(
            'insert into defaultmodel ("int_default","float_default","decimal_default",'
            '"bool_default","char_default","date_default","datetime_default") '
            "values (DEFAULT,DEFAULT,DEFAULT,DEFAULT,DEFAULT,DEFAULT,DEFAULT)",
        )
    yield


@pytest.mark.asyncio
async def test_default(default_row):
    """Test that default values are correctly applied when inserting via raw SQL."""
    default_model = await DefaultModel.objects.first()
    assert default_model.int_default == 1
    assert default_model.float_default == 1.5
    assert default_model.decimal_default == Decimal(1)
    assert default_model.bool_default
    assert default_model.char_default == "hare"
    assert default_model.date_default == datetime.date(year=2020, month=5, day=21)
    assert default_model.datetime_default == datetime.datetime(year=2020, month=5, day=20, tzinfo=UTC)
