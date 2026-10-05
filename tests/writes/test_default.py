import datetime
from decimal import Decimal

import pytest
import pytest_asyncio

from hare import Connections
from hare.dialects.postgresql.client import PostgresqlClient
from hare.dialects.sqlite.client import SqliteClient
from hare.time import UTC
from tests.shared_models.testmodels import DefaultModel


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


@pytest.mark.asyncio
async def test_a_decimal_default_is_stored_as_the_text_hare_writes(default_row):
    """SQLite keeps a decimal as its text: the default, written as a number, was stored as a float's text
    (``1.0`` for ``1.00``) - inspectdb then read it back as a float a CharField can't take."""
    db_conn = Connections.get("models")
    if not isinstance(db_conn, SqliteClient):
        pytest.skip("SQLite keeps a decimal as text")
    _, rows = await db_conn.execute("SELECT decimal_default FROM defaultmodel")
    assert rows[0]["decimal_default"] == "1.00"
