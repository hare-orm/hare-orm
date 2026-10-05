"""A raw statement the database or the driver refuses raises the error of the statement - the
connection it ran on stays usable."""

import datetime

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import OperationalError
from hare.query.expressions import RawSQL
from hare.transactions.transactions import Transactions
from tests.testmodels import IntFields


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_statement_given_another_number_of_arguments_is_a_statement_error(db):
    """asyncpg refuses it before sending, with an InterfaceError - read as a lost connection."""
    for sql, parameters in (("SELECT $1::int + $2::int AS total", [1]), ("SELECT $1::int AS total", [1, 2])):
        with pytest.raises(OperationalError) as error_info:
            # A savepoint - the server's own refusal aborts the transaction around it.
            async with Transactions.atomic():
                await db.get_connection().execute(sql, parameters, returns_rows=True)
        assert type(error_info.value) is OperationalError
    _row_count, rows = await db.get_connection().execute("SELECT 1 AS one", [], returns_rows=True)
    assert rows[0]["one"] == 1


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT interval '178000000 years'",
        "SELECT '24:00:00'::time",
        "SELECT '5874897-01-01'::date",
        "SELECT '0044-03-15 BC'::timestamptz",
    ],
)
async def test_a_value_python_holds_none_of_is_a_statement_error(db, sql):
    """asyncpg's codecs raised a bare ValueError or OverflowError - and read an interval of more than
    about 5.9 million years as a wrong timedelta; rust_pg, decoding a row as it is read, raised
    Python's ValueError of a year before 1 or its own ConversionError."""
    await IntFields.objects.create(intnum=1)
    with pytest.raises(OperationalError):
        await IntFields.objects.annotate(value=RawSQL(sql.removeprefix("SELECT "), [])).values_list("value", flat=True)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_an_interval_is_read_and_written_as_before(db):
    connection = db.get_connection()
    _row_count, rows = await connection.execute(
        "SELECT interval '1 year 2 months 3 days 04:05:06.789' AS long, interval '-1 mon -3 days -1 sec' AS back, "
        "$1::interval::text AS written",
        [datetime.timedelta(days=3, seconds=5)],
        returns_rows=True,
    )
    assert (rows[0]["long"], rows[0]["back"], rows[0]["written"]) == (
        datetime.timedelta(days=428, seconds=14706, microseconds=789000),
        datetime.timedelta(days=-34, seconds=86399),
        "3 days 00:00:05",
    )
