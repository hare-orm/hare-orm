"""TransactionRetryError - a statement the database aborted because of a concurrent transaction, which
the connection's driver says can succeed when the whole transaction runs again."""

import sqlite3

import asyncpg
import pytest

from hare.contrib.test import requires_features
from hare.dialects.postgresql.drivers.asyncpg.asyncpg_driver import AsyncpgDriver
from hare.dialects.sqlite.drivers.aiosqlite.aiosqlite_driver import AiosqliteDriver
from hare.exceptions import IntegrityError, OperationalError, TransactionRetryError
from hare.transactions.enums import IsolationLevel
from hare.transactions.transactions import Transactions
from tests.shared_models.testmodels import Tournament


class ServerErrorWithSqlstate(Exception):
    """A driver exception carrying the server's SQLSTATE, as rust_pg's exceptions do."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"server error {sqlstate}")
        self.sqlstate = sqlstate


def get_sqlite_busy_error(tmp_path) -> sqlite3.OperationalError:
    """A real "database is locked" error: a second connection asks for the write lock the first holds."""
    database_path = tmp_path / "locked.sqlite3"
    holder = sqlite3.connect(database_path, timeout=0, isolation_level=None)
    waiter = sqlite3.connect(database_path, timeout=0, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError) as error_info:
            waiter.execute("BEGIN IMMEDIATE")
        return error_info.value
    finally:
        holder.close()
        waiter.close()


@pytest.mark.database_independent
@pytest.mark.parametrize(
    ("error", "is_retryable"),
    [
        (asyncpg.exceptions.SerializationError("could not serialize access"), True),
        (asyncpg.exceptions.DeadlockDetectedError("deadlock detected"), True),
        (asyncpg.exceptions.TransactionIntegrityConstraintViolationError("integrity"), False),
        (asyncpg.exceptions.LockNotAvailableError("could not obtain lock"), False),
        (asyncpg.exceptions.QueryCanceledError("canceling statement"), False),
        (ServerErrorWithSqlstate("40001"), True),
        (ServerErrorWithSqlstate("40P01"), True),
        (ServerErrorWithSqlstate("40003"), False),
        (ServerErrorWithSqlstate("23505"), False),
        (ValueError("no sqlstate"), False),
    ],
)
def test_postgresql_driver_classifies_by_sqlstate(error, is_retryable):
    assert AsyncpgDriver().is_retryable(error) is is_retryable


@pytest.mark.parametrize(
    ("message", "is_retryable"),
    [
        ("Code: 650. DB::Exception: Serialization error: part all_1_1_0 is locked by transaction 1", True),
        (
            "Code: 341. DB::Exception: Exception happened during execution of mutation 'mutation_18.txt' with part "
            "'all_1_10_1_16' reason: 'Serialization error: part all_1_10_1_16 is locked by transaction 17'",
            True,
        ),
        ("Code: 341. DB::Exception: Exception happened during execution of mutation: Memory limit exceeded", False),
        ("Code: 60. DB::Exception: Unknown table", False),
        ("no code at all", False),
    ],
)
def test_clickhouse_drivers_retry_a_serialization_error(message, is_retryable):
    # A transaction's statement meeting a part a background merge holds - the whole transaction can run again.
    from hare.dialects.clickhouse.drivers.clickhouse_connect.clickhouse_connect_driver import ClickhouseConnectDriver
    from hare.dialects.clickhouse.drivers.clickhouse_driver.clickhouse_driver_driver import ClickhouseDriverDriver

    for driver in (ClickhouseConnectDriver(), ClickhouseDriverDriver()):
        assert driver.is_retryable(Exception(message)) is is_retryable


@pytest.mark.database_independent
def test_sqlite_driver_retries_a_busy_database(tmp_path):
    busy_error = get_sqlite_busy_error(tmp_path)
    assert AiosqliteDriver().is_retryable(busy_error)


@pytest.mark.database_independent
@pytest.mark.parametrize(
    "error",
    [
        sqlite3.OperationalError("no such table: missing"),
        sqlite3.IntegrityError("UNIQUE constraint failed"),
        OSError(),
    ],
)
def test_sqlite_driver_does_not_retry_other_errors(error):
    assert not AiosqliteDriver().is_retryable(error)


@pytest.mark.asyncio
async def test_sqlite_client_translates_a_busy_database(db, tmp_path):
    busy_error = get_sqlite_busy_error(tmp_path)
    client = Tournament._meta.connection
    if client.dialect.name != "sqlite":
        pytest.skip("translates through the SQLite client")
    translated = client._get_operational_error(busy_error, sql="BEGIN IMMEDIATE")
    assert type(translated) is TransactionRetryError
    assert translated.sql == "BEGIN IMMEDIATE"
    other = client._get_operational_error(sqlite3.OperationalError("no such table: missing"))
    assert type(other) is OperationalError


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_concurrent_update_under_repeatable_read_raises_transaction_retry_error(db_truncate):
    tournament = await Tournament.objects.create(name="first")
    with pytest.raises(TransactionRetryError, match="40001|could not serialize") as error_info:
        async with Transactions.atomic(isolation=IsolationLevel.REPEATABLE_READ):
            # The first statement takes the transaction's snapshot.
            await Tournament.objects.get(id=tournament.id)
            async with Transactions.autonomous() as other_connection:
                await Tournament.objects.filter(id=tournament.id).using(other_connection).update(name="concurrent")
            await Tournament.objects.filter(id=tournament.id).update(name="stale")
    assert isinstance(error_info.value, OperationalError)
    assert not isinstance(error_info.value, IntegrityError)
    assert (await Tournament.objects.get(id=tournament.id)).name == "concurrent"


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_retrying_the_transaction_succeeds(db_truncate):
    tournament = await Tournament.objects.create(name="first")
    attempts = 0
    while True:
        attempts += 1
        try:
            async with Transactions.atomic(isolation=IsolationLevel.REPEATABLE_READ):
                current = await Tournament.objects.get(id=tournament.id)
                if attempts == 1:
                    async with Transactions.autonomous() as other_connection:
                        await (
                            Tournament.objects.filter(id=tournament.id)
                            .using(other_connection)
                            .update(name="concurrent")
                        )
                await Tournament.objects.filter(id=tournament.id).update(name=f"{current.name}+retried")
        except TransactionRetryError:
            continue
        break
    assert attempts == 2
    assert (await Tournament.objects.get(id=tournament.id)).name == "concurrent+retried"


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_plain_query_error_stays_operational_error(db_truncate):
    with pytest.raises(OperationalError) as error_info:
        await Tournament._meta.connection.execute("SELECT * FROM missing_table_for_retry_test")
    assert type(error_info.value) is OperationalError
