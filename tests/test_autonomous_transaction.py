import asyncio
import time

import pytest
import pytest_asyncio

from hare.contrib.test import requires_features
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.core.connections.connections import Connections
from hare.exceptions import IntegrityError, OperationalError, TransactionManagementError
from hare.transactions.transactions import Transactions
from tests.testmodels import CompositePkThing, IntFields


@pytest_asyncio.fixture
async def file_db(tmp_path):
    """A real file-backed SQLite DB, not `:memory:` - a fresh connection to `:memory:` is a
    completely separate, empty database, which would make every test below fail for reasons
    unrelated to Transactions.autonomous() itself. autonomous() is meant for real persistent
    databases (file SQLite, Postgres), where a second connection genuinely shares state with
    the first - exactly what these tests need to verify."""
    db_path = tmp_path / "autonomous_test.sqlite"
    async with hare_test_context(
        ["tests.testmodels"], db_url=f"sqlite+aiosqlite:///{db_path}?synchronous=OFF", connection_label="models"
    ) as ctx:
        yield ctx


@pytest.mark.asyncio
async def test_autonomous_write_is_visible_after_block(file_db):
    async with Transactions.autonomous() as conn:
        await IntFields.objects.using(conn).create(intnum=1)

    assert await IntFields.objects.filter(intnum=1).exists()


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_autonomous_write_survives_enclosing_transaction_rollback(file_db):
    """The whole point - a write on the autonomous connection commits independently of an
    enclosing transaction, so it must still be there even after that transaction rolls back."""
    # No write on the OUTER transaction's own connection here, deliberately - SQLite serializes
    # writers (even in WAL mode), so a concurrent write attempt on the outer connection while the
    # autonomous one is also writing would just block on "database is locked" instead of
    # demonstrating the actual thing being tested: that a write on the independent connection
    # persists regardless of what happens to a (here, empty) enclosing transaction.
    with pytest.raises(IntegrityError):
        async with Transactions.atomic():
            async with Transactions.autonomous() as conn:
                await IntFields.objects.using(conn).create(intnum=2)
            raise IntegrityError("forced rollback")

    assert await IntFields.objects.filter(intnum=2).exists()


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_autonomous_connection_unaffected_by_enclosing_atomic(file_db):
    async with Transactions.atomic():
        async with Transactions.autonomous() as conn:
            await IntFields.objects.using(conn).create(intnum=42)
        # visible immediately, from inside the still-open enclosing transaction's own connection,
        # since it's a genuinely separate, already-committed write - not dependent on the outer
        # transaction ever committing.
        assert await IntFields.objects.filter(intnum=42).exists()


@pytest.mark.asyncio
async def test_autonomous_closes_connection_after_block(file_db):
    async with Transactions.autonomous() as conn:
        await IntFields.objects.using(conn).create(intnum=1)
        assert conn._connection is not None

    assert conn._connection is None


@pytest.mark.asyncio
async def test_autonomous_accepts_explicit_connection_name(file_db):
    alias = next(iter(Connections.current().db_config))
    async with Transactions.autonomous(using=alias) as conn:
        await IntFields.objects.using(conn).create(intnum=7)

    assert await IntFields.objects.filter(intnum=7).exists()


@pytest.mark.asyncio
async def test_autonomous_yields_independent_client_instance(db):
    """Not the same cached client object .get() would return - a genuinely separate connection.
    No actual query is issued here, so the regular in-memory `db` fixture is fine."""
    alias = next(iter(Connections.current().db_config))
    shared = Connections.get(alias)
    async with Transactions.autonomous() as conn:
        assert conn is not shared


# ============================================================================
# Further combinations: bulk_create()/bulk_update(), composite PK, nested autonomous()
# ============================================================================


@pytest.mark.asyncio
async def test_autonomous_bulk_create(file_db):
    async with Transactions.autonomous() as conn:
        await IntFields.objects.using(conn).bulk_create([IntFields(intnum=1), IntFields(intnum=2)])

    assert await IntFields.objects.all().count() == 2


@pytest.mark.asyncio
async def test_autonomous_bulk_update(file_db):
    a = await IntFields.objects.create(intnum=1)
    b = await IntFields.objects.create(intnum=2)
    a.intnum = 10
    b.intnum = 20

    async with Transactions.autonomous() as conn:
        count = await IntFields.objects.using(conn).bulk_update([a, b], fields=["intnum"])

    assert count == 2
    assert sorted(row.intnum for row in await IntFields.objects.all()) == [10, 20]


@pytest.mark.asyncio
async def test_autonomous_composite_pk_create(file_db):
    async with Transactions.autonomous() as conn:
        await CompositePkThing.objects.using(conn).create(thing_id=1, revision=1, name="X")

    assert await CompositePkThing.objects.filter(thing_id=1, revision=1).exists()


@pytest.mark.asyncio
async def test_nested_autonomous_both_writes_persist(file_db):
    """An autonomous() block opened from inside another autonomous() block - each is its own
    independent connection/commit boundary, nesting them doesn't merge or interfere."""
    async with Transactions.autonomous() as outer_conn:
        await IntFields.objects.using(outer_conn).create(intnum=1)
        async with Transactions.autonomous() as inner_conn:
            await IntFields.objects.using(inner_conn).create(intnum=2)
        assert inner_conn is not outer_conn

    assert sorted(row.intnum for row in await IntFields.objects.all()) == [1, 2]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_autonomous_within_nested_savepoint_does_not_hang(file_db):
    """The documented on_rollback() + autonomous() compensating-action pattern (see
    docs/connections/transactions.md), where the compensating write needs its own
    _in_transaction() because it's more than one statement, called from code running inside an
    already-open nested savepoint on the OUTER connection. Reading that outer savepoint's own
    span off current_savepoint_span and handing it straight to the autonomous connection's own
    NestedSavepointLock used to wait AMBIENT_QUERY_SAVEPOINT_WAIT_TIMEOUT_SECONDS (30s) for a
    span that lock could never treat as its own topmost span, then raise a misleading "concurrent
    gather()/TaskGroup sibling" TransactionManagementError - even though every line here runs
    sequentially, in one task. Wrapped in a tight wait_for so a regression fails in seconds, not
    30+.

    No write on the outer connection's own nested savepoint here, deliberately (staying open
    with the block otherwise empty is enough to reproduce the span bug) - SQLite serializes
    writers even across independent connections to the same file, so a concurrent write on the
    outer connection while the autonomous one is also writing would just block on "database is
    locked" instead, unrelated to what this test is actually about."""
    start = time.monotonic()

    async def scenario() -> None:
        async with Transactions.atomic() as outer:
            async with outer._in_transaction():
                async with Transactions.autonomous() as other_conn:
                    async with other_conn._in_transaction() as other_tx:
                        await IntFields.objects.using(other_tx).create(intnum=2)

    await asyncio.wait_for(scenario(), timeout=10)

    elapsed = time.monotonic() - start
    assert elapsed < 5, f"should complete near-instantly, took {elapsed:.1f}s - the cross-connection span bug is back"
    assert await IntFields.objects.filter(intnum=2).exists()


SQLITE_LONG_COUNT_SQL = (
    "WITH RECURSIVE counter(value) AS (SELECT 1 UNION ALL SELECT value + 1 FROM counter WHERE value < 1000000000) "
    "SELECT count(*) FROM counter"
)


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_autonomous_read_only_and_time_limited_on_sqlite(file_db):
    await IntFields.objects.create(intnum=1)
    async with Transactions.atomic():
        async with Transactions.autonomous(read_only=True) as read_only_connection:
            assert await IntFields.objects.all().using(read_only_connection).count() == 1
        with pytest.raises(TransactionManagementError, match="read-only transaction"):
            async with Transactions.autonomous(read_only=True) as read_only_connection:
                await IntFields.objects.using(read_only_connection).create(intnum=2)
        with pytest.raises(OperationalError, match="statement timeout"):
            async with Transactions.autonomous(statement_timeout=0.2) as limited_connection:
                await limited_connection.execute(SQLITE_LONG_COUNT_SQL)
    await IntFields.objects.create(intnum=3)
    assert sorted(await IntFields.objects.all().values_list("intnum", flat=True)) == [1, 3]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_autonomous_read_only_and_time_limited_on_postgresql(db_isolated):
    await IntFields.objects.create(intnum=1)
    async with Transactions.atomic("models"):
        async with Transactions.autonomous("models", read_only=True) as read_only_connection:
            assert await IntFields.objects.all().using(read_only_connection).count() == 1
        with pytest.raises(TransactionManagementError, match="read-only transaction"):
            async with Transactions.autonomous("models", read_only=True) as read_only_connection:
                await IntFields.objects.using(read_only_connection).create(intnum=2)
        with pytest.raises(OperationalError):
            async with Transactions.autonomous("models", statement_timeout=0.2) as limited_connection:
                await limited_connection.execute("SELECT pg_sleep(5)")
    await IntFields.objects.create(intnum=3)
    assert sorted(await IntFields.objects.all().values_list("intnum", flat=True)) == [1, 3]
