"""rust_pg sends a transaction's BEGIN with its first statement, and a transaction that runs no
statement never reaches the server."""

import pytest

from hare.contrib.test import requires_features
from hare.transactions.transactions import Transactions
from tests.testmodels import Tournament

IDLE_IN_TRANSACTION_COUNT_SQL = (
    "SELECT count(*) AS count FROM pg_stat_activity "
    "WHERE datname = current_database() AND state LIKE 'idle in transaction%'"
)


class RollBackNested(Exception):
    """Rolls the nested block back."""


def skip_unless_rust_pg() -> None:
    if Tournament.get_connection().driver_name != "postgresql":
        pytest.skip("rust_pg only - asyncpg sends BEGIN when the transaction starts")


async def count_idle_in_transaction(pool_client) -> int:
    rows = await pool_client.execute_dicts(IDLE_IN_TRANSACTION_COUNT_SQL)
    return rows[0]["count"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_transaction_without_statements_never_begins_on_the_server(db_truncate):
    skip_unless_rust_pg()
    pool_client = Tournament.get_connection()
    async with Transactions.atomic():
        assert await count_idle_in_transaction(pool_client) == 0
    async with Transactions.atomic() as transaction:
        await transaction.execute("SELECT 1")
        assert await count_idle_in_transaction(pool_client) == 1
    assert await count_idle_in_transaction(pool_client) == 0


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_empty_transaction_rolled_back_by_an_error(db_truncate):
    skip_unless_rust_pg()
    with pytest.raises(RollBackNested):
        async with Transactions.atomic():
            raise RollBackNested
    await Tournament.objects.create(name="after")
    assert await Tournament.objects.all().count() == 1


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_isolation_level_applies_to_the_first_statement(db_truncate):
    async with Transactions.atomic(isolation="serializable") as transaction:
        rows = await transaction.execute_dicts("SHOW transaction_isolation")
    assert rows[0]["transaction_isolation"] == "serializable"


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_savepoint_as_the_first_statement(db_truncate):
    async with Transactions.atomic():
        with pytest.raises(RollBackNested):
            async with Transactions.atomic():
                await Tournament.objects.create(name="rolled back")
                raise RollBackNested
        await Tournament.objects.create(name="kept")
    assert [tournament.name for tournament in await Tournament.objects.all()] == ["kept"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_failing_first_statement_rolls_the_transaction_back(db_truncate):
    await Tournament.objects.create(name="existing")
    with pytest.raises(Exception):  # noqa: B017 - the driver's own error for a bad statement
        async with Transactions.atomic() as transaction:
            await transaction.execute("SELECT * FROM no_such_table")
    await Tournament.objects.create(name="after")
    assert await Tournament.objects.all().count() == 2


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_writes_of_a_committed_transaction_persist(db_truncate):
    async with Transactions.atomic():
        await Tournament.objects.create(name="first")
        await Tournament.objects.create(name="second")
    assert await Tournament.objects.all().count() == 2
