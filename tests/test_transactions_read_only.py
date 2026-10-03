"""Transactions.atomic(read_only=..., statement_timeout=...) - the database itself refuses
writes and cancels over-running statements, on every backend."""

import re
import time

import pytest

from hare.contrib.test import requires_features
from hare.dialects.base.client import TransactionClient
from hare.exceptions import OperationalError, QueryError, TransactionManagementError
from hare.instrumentation.observer_dispatch import ObserverDispatch
from hare.instrumentation.observers import Observers
from hare.instrumentation.query_executed import QueryExecuted
from hare.instrumentation.transaction_event import TransactionEvent
from hare.transactions.enums import TransactionEventType
from hare.transactions.transactions import Transactions
from tests.testmodels import Tournament

READ_ONLY_REJECTION = re.compile(r"read-?only", re.IGNORECASE)


@pytest.fixture(autouse=True)
def _clear_instrumentation_hooks():
    Observers.process_observers.clear()
    ObserverDispatch.pending_errors.clear()
    yield
    Observers.process_observers.clear()
    ObserverDispatch.pending_errors.clear()


def _dialect() -> str:
    return Tournament._meta.db.dialect.name


def _slow_query() -> str:
    if _dialect() == "sqlite":
        return (
            "WITH RECURSIVE counter(value) AS (SELECT 1 UNION ALL SELECT value + 1 FROM counter) "
            "SELECT count(*) FROM counter"
        )
    return "SELECT pg_sleep(5)"


async def _assert_query_only_off() -> None:
    if _dialect() == "sqlite":
        _, rows = await Tournament._meta.db.execute("PRAGMA query_only")
        assert rows[0][0] == 0


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_orm_write_inside_read_only_transaction_is_rejected_by_database(db_truncate):
    with pytest.raises((OperationalError, TransactionManagementError), match=READ_ONLY_REJECTION):
        async with Transactions.atomic(read_only=True):
            await Tournament.objects.create(name="rejected")
    assert await Tournament.objects.all().count() == 0


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_raw_write_inside_read_only_transaction_is_rejected_by_database(db_truncate):
    """Raw SQL bypasses every ORM code path - only the database itself can refuse it."""
    with pytest.raises((OperationalError, TransactionManagementError), match=READ_ONLY_REJECTION):
        async with Transactions.atomic(read_only=True) as connection:
            await connection.execute("DELETE FROM tournament")


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_read_inside_read_only_transaction_works(db_truncate):
    await Tournament.objects.create(name="existing")
    async with Transactions.atomic(read_only=True) as connection:
        assert await Tournament.objects.all().values_list("name", flat=True) == ["existing"]
        _, rows = await connection.execute("SELECT count(*) AS total FROM tournament")
        assert rows[0]["total"] == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_write_works_again_after_read_only_block(db_truncate):
    async with Transactions.atomic(read_only=True):
        await Tournament.objects.all().count()
    await _assert_query_only_off()
    await Tournament.objects.create(name="after")
    async with Transactions.atomic():
        await Tournament.objects.create(name="after in transaction")
    assert await Tournament.objects.all().count() == 2


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_write_works_again_after_exception_inside_read_only_block(db_truncate):
    with pytest.raises(ValueError):
        async with Transactions.atomic(read_only=True):
            raise ValueError("boom")
    await _assert_query_only_off()
    await Tournament.objects.create(name="after exception")
    assert await Tournament.objects.all().count() == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_write_works_again_after_rejected_write_inside_read_only_block(db_truncate):
    with pytest.raises((OperationalError, TransactionManagementError)):
        async with Transactions.atomic(read_only=True):
            await Tournament.objects.create(name="rejected")
    await _assert_query_only_off()
    await Tournament.objects.create(name="after rejection")
    assert await Tournament.objects.all().count() == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_write_works_after_manual_commit_and_rollback_of_read_only_transaction(db_truncate):
    async with Transactions.atomic(read_only=True) as connection:
        await connection.commit()
    await _assert_query_only_off()
    async with Transactions.atomic(read_only=True) as connection:
        await connection.rollback()
    await _assert_query_only_off()
    await Tournament.objects.create(name="after manual end")
    assert await Tournament.objects.all().count() == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_on_commit_callback_of_read_only_transaction_can_write(db_truncate):
    async def write_after_commit() -> None:
        await Tournament.objects.create(name="from callback")

    async with Transactions.atomic(read_only=True):
        Transactions.on_commit(write_after_commit)
    assert await Tournament.objects.all().values_list("name", flat=True) == ["from callback"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_statement_timeout_cancels_long_statement(db_truncate):
    started_at = time.monotonic()
    with pytest.raises(OperationalError):
        async with Transactions.atomic(statement_timeout=0.2) as connection:
            await connection.execute(_slow_query())
    assert time.monotonic() - started_at < 4
    await Tournament.objects.create(name="after timeout")
    assert await Tournament.objects.all().count() == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_statement_timeout_with_read_only(db_truncate):
    with pytest.raises(OperationalError):
        async with Transactions.atomic(read_only=True, statement_timeout=0.2) as connection:
            await connection.execute(_slow_query())
    await _assert_query_only_off()
    await Tournament.objects.create(name="after")


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_statement_timeout_does_not_affect_fast_statements(db_truncate):
    await Tournament.objects.create(name="existing")
    async with Transactions.atomic(statement_timeout=5) as connection:
        for _ in range(3):
            _, rows = await connection.execute("SELECT count(*) AS total FROM tournament")
            assert rows[0]["total"] == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_statement_timeout_applies_inside_nested_transaction(db_truncate):
    with pytest.raises(OperationalError):
        async with Transactions.atomic(statement_timeout=0.2):
            async with Transactions.atomic() as nested:
                await nested.execute(_slow_query())


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_statement_timeout_does_not_outlive_transaction(db_truncate):
    async with Transactions.atomic(statement_timeout=0.05):
        pass
    fast_enough_but_over_timeout = "SELECT 1" if _dialect() == "sqlite" else "SELECT pg_sleep(0.2)"
    await Tournament._meta.db.execute(fast_enough_but_over_timeout)
    async with Transactions.atomic() as connection:
        await connection.execute(fast_enough_but_over_timeout)


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("statement_timeout", [0.75, 1, 1.5])
async def test_postgres_timeout_settings_are_transaction_local_milliseconds(db_truncate, statement_timeout):
    if _dialect() == "sqlite":
        pytest.skip("Postgres-only settings")
    expected = f"{round(statement_timeout * 1000)}ms"
    async with Transactions.atomic(statement_timeout=statement_timeout) as connection:
        _, statement_rows = await connection.execute("SELECT current_setting('statement_timeout') AS value")
        _, lock_rows = await connection.execute("SELECT current_setting('lock_timeout') AS value")
        _, read_only_rows = await connection.execute("SELECT current_setting('transaction_read_only') AS value")
    assert statement_rows[0]["value"] in {expected, f"{statement_timeout:g}s"}
    assert lock_rows[0]["value"] == statement_rows[0]["value"]
    assert read_only_rows[0]["value"] == "off"
    _, outside_rows = await Tournament._meta.db.execute("SELECT current_setting('statement_timeout') AS value")
    assert outside_rows[0]["value"] != statement_rows[0]["value"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_restriction_statements_reach_query_instrumentation(db_truncate):
    executed_sql: list[str] = []
    transaction_events: list[TransactionEvent] = []
    Observers.observe(QueryExecuted, lambda event: executed_sql.append(event.sql))
    Observers.observe(TransactionEvent, lambda event: transaction_events.append(event.type))

    async with Transactions.atomic(read_only=True, statement_timeout=2):
        await Tournament.objects.all().count()
    await Observers.wait_for_pending()
    await Observers.wait_for_pending()

    joined_sql = "\n".join(executed_sql)
    if _dialect() == "sqlite":
        assert "PRAGMA query_only = ON" in executed_sql[0]
        assert "PRAGMA query_only = OFF" in executed_sql[-1]
    else:
        assert "SET TRANSACTION READ ONLY" in executed_sql[0]
        assert "SET LOCAL statement_timeout = 2000" in joined_sql
        assert "SET LOCAL lock_timeout = 2000" in joined_sql
    assert any("tournament" in sql.lower() for sql in executed_sql)
    assert transaction_events == [TransactionEventType.BEGIN, TransactionEventType.COMMIT]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_rejected_write_fires_rollback_transaction_event(db_truncate):
    transaction_events: list[TransactionEvent] = []
    Observers.observe(TransactionEvent, lambda event: transaction_events.append(event.type))
    with pytest.raises((OperationalError, TransactionManagementError)):
        async with Transactions.atomic(read_only=True):
            await Tournament.objects.create(name="rejected")
    await Observers.wait_for_pending()
    assert transaction_events == [TransactionEventType.BEGIN, TransactionEventType.ROLLBACK]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_plain_transaction_runs_no_restriction_statements(db_truncate):
    executed_sql: list[str] = []
    Observers.observe(QueryExecuted, lambda event: executed_sql.append(event.sql))
    async with Transactions.atomic():
        pass
    await Observers.wait_for_pending()
    assert executed_sql == []


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_nested_read_only_inside_read_write_raises(db_truncate):
    async with Transactions.atomic():
        await Tournament.objects.create(name="outer")
        with pytest.raises(QueryError):
            async with Transactions.atomic(read_only=True):
                pass
    assert await Tournament.objects.all().count() == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_nested_read_only_inside_read_only_is_a_savepoint(db_truncate):
    await Tournament.objects.create(name="existing")
    async with Transactions.atomic(read_only=True):
        async with Transactions.atomic(read_only=True) as nested:
            assert isinstance(nested, TransactionClient)
            assert await Tournament.objects.all().count() == 1
        with pytest.raises((OperationalError, TransactionManagementError), match=READ_ONLY_REJECTION):
            async with Transactions.atomic(read_only=True):
                await Tournament.objects.create(name="rejected")
        assert await Tournament.objects.all().count() == 1
    await _assert_query_only_off()


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_plain_nested_inside_read_only_stays_read_only(db_truncate):
    async with Transactions.atomic(read_only=True):
        with pytest.raises((OperationalError, TransactionManagementError), match=READ_ONLY_REJECTION):
            async with Transactions.atomic():
                await Tournament.objects.create(name="rejected")
        assert await Tournament.objects.all().count() == 0
    await Tournament.objects.create(name="after")
    assert await Tournament.objects.all().count() == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("outer_read_only", [False, True])
async def test_nested_statement_timeout_raises(db_truncate, outer_read_only):
    async with Transactions.atomic(read_only=outer_read_only, statement_timeout=5):
        with pytest.raises(QueryError):
            async with Transactions.atomic(statement_timeout=1):
                pass
        with pytest.raises(QueryError):
            async with Transactions.atomic(read_only=outer_read_only, statement_timeout=5):
                pass


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_atomic_passes_restrictions(db_truncate):
    @Transactions.atomic(read_only=True)
    async def write_in_read_only() -> None:
        await Tournament.objects.create(name="rejected")

    @Transactions.atomic(read_only=True, statement_timeout=5)
    async def read_in_read_only() -> int:
        return await Tournament.objects.all().count()

    with pytest.raises((OperationalError, TransactionManagementError), match=READ_ONLY_REJECTION):
        await write_in_read_only()
    assert await read_in_read_only() == 0
    await Tournament.objects.create(name="after")


@pytest.mark.parametrize(
    ("read_only", "statement_timeout"),
    [
        ("yes", None),
        (1, None),
        (False, 0),
        (False, -1),
        (False, True),
        (False, "5"),
        (False, 0.0001),
        (False, 10**9),
        (False, float("nan")),
        (False, float("inf")),
    ],
)
def test_invalid_restriction_arguments_raise_params_error(read_only, statement_timeout):
    with pytest.raises(QueryError):
        Transactions.atomic(read_only=read_only, statement_timeout=statement_timeout)


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_failed_restriction_statement_ends_transaction_cleanly(db_truncate, monkeypatch):
    transaction_events: list[TransactionEvent] = []
    Observers.observe(TransactionEvent, lambda event: transaction_events.append(event.type))

    async def failing_restrictions(self) -> None:
        await self.execute("SELECT * FROM hare_no_such_table")

    monkeypatch.setattr(TransactionClient, "_apply_transaction_restrictions", failing_restrictions)
    with pytest.raises(OperationalError):
        async with Transactions.atomic(read_only=True):
            pytest.fail("the block must not run")
    monkeypatch.undo()
    await Observers.wait_for_pending()
    assert transaction_events == [TransactionEventType.BEGIN, TransactionEventType.ROLLBACK]

    async with Transactions.atomic():
        await Tournament.objects.create(name="after failed start")
    assert await Tournament.objects.all().count() == 1
