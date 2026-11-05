"""Native transaction entry acknowledges BEGIN and preserves statement outcomes."""

import pytest

from hare import Connections
from hare.contrib.test import requires_features
from hare.transactions.transactions import Transactions
from tests.shared_models.testmodels import Tournament
from tests.utils.database_under_test import DatabaseUnderTest

IDLE_IN_TRANSACTION_COUNT_SQL = (
    "SELECT count(*) AS count FROM pg_stat_activity "
    "WHERE datname = current_database() AND state LIKE 'idle in transaction%'"
)


class RollBackNested(Exception):
    """Rolls the nested block back."""


def skip_unless_rust_pg() -> None:
    if Tournament.get_connection().driver_name != "postgresql":
        pytest.skip("native PostgreSQL transaction entry")


async def count_idle_in_transaction(pool_client) -> int:
    rows = await pool_client.execute_dicts(IDLE_IN_TRANSACTION_COUNT_SQL)
    return rows[0]["count"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_transaction_entry_confirms_begin_before_the_first_statement(db_truncate):
    skip_unless_rust_pg()
    pool_client = Tournament.get_connection()
    async with Transactions.atomic():
        assert await count_idle_in_transaction(pool_client) == 1
    async with Transactions.atomic() as transaction:
        await transaction.execute("SELECT 1")
        assert await count_idle_in_transaction(pool_client) == 1
    assert await count_idle_in_transaction(pool_client) == 0


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_direct_native_begin_confirms_the_server_transaction(db_truncate):
    skip_unless_rust_pg()
    client = Tournament.get_connection()
    await client._ensure_connection()
    transaction = await client._connected_pool.begin(None)
    try:
        assert await count_idle_in_transaction(client) == 1
    finally:
        await transaction.rollback()
    assert await count_idle_in_transaction(client) == 0


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_stale_plan_message_does_not_repeat_a_statement_with_effects(db_truncate):
    skip_unless_rust_pg()
    if DatabaseUnderTest.pools_server_sessions():
        pytest.skip("session-local sequence and function require a pinned server session")
    client = Connections.current().create_independent(
        "models", {"statement_cache_size": 4, "min_size": 1, "max_size": 1}
    )
    try:
        await client.execute_script("""
            CREATE TEMP SEQUENCE statement_attempts;
            CREATE FUNCTION pg_temp.fail_after_effect() RETURNS bigint LANGUAGE plpgsql AS $$
            BEGIN
                PERFORM nextval('pg_temp.statement_attempts');
                RAISE EXCEPTION 'cached plan must not change result type' USING ERRCODE = '0A000';
            END $$;
        """)
        with pytest.raises(Exception, match="cached plan must not change result type"):
            await client.execute("SELECT pg_temp.fail_after_effect()")
        # Sequence increments survive the failed statement, exposing any hidden replay.
        assert await client.execute_dicts("SELECT currval('pg_temp.statement_attempts') AS attempts") == [
            {"attempts": 1}
        ]
    finally:
        await client.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_stale_plan_is_evicted_for_the_next_explicit_call(db_truncate):
    skip_unless_rust_pg()
    if DatabaseUnderTest.pools_server_sessions():
        pytest.skip("session-local table requires a pinned server session")
    client = Connections.current().create_independent(
        "models", {"statement_cache_size": 8, "min_size": 1, "max_size": 1}
    )
    try:
        await client.execute("CREATE TEMP TABLE changing_result (value integer)")
        assert await client.execute_dicts("SELECT * FROM changing_result") == []
        await client.execute("ALTER TABLE changing_result ADD COLUMN label text")
        with pytest.raises(Exception, match="cached plan must not change result type"):
            await client.execute_dicts("SELECT * FROM changing_result")
        assert await client.execute_dicts("SELECT * FROM changing_result") == []
    finally:
        await client.close()


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
