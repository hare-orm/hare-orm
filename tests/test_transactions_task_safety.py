"""Transactions shared by several asyncio tasks: cancellation of COMMIT, sibling statements racing
the COMMIT, callbacks registered by siblings, contexts exited from another task, hooks and lost
connections."""

import asyncio
import logging
import time

import pytest

from hare.contrib.test import requires_features
from hare.core.connections import Connections
from hare.exceptions import DBConnectionError, TransactionManagementError
from hare.instrumentation.observers import Observers
from hare.instrumentation.query_executed import QueryExecuted
from hare.instrumentation.transaction_event import TransactionEvent
from hare.transactions.enums import TransactionEventType
from hare.transactions.transactions import Transactions
from tests.testmodels import Author, Tournament

TEST_TIMEOUT_SECONDS = 20
POOL_CLOSE_TIME_LIMIT_SECONDS = 2


class Boom(Exception):
    """A very specific exception so as to not accidentally catch another exception."""


def _is_sqlite() -> bool:
    return Tournament._meta.db.dialect.name == "sqlite"


def _slow_query_sql(seconds: float) -> str:
    """A read-only statement that runs for roughly ``seconds``."""
    if _is_sqlite():
        row_count = int(seconds * 3_000_000)
        return (
            "WITH RECURSIVE counter(value) AS (SELECT 1 UNION ALL SELECT value + 1 FROM counter "
            f"WHERE value < {row_count}) SELECT count(*) FROM counter"
        )
    return f"SELECT pg_sleep({seconds})"


async def _wait_until(condition) -> None:
    while not condition():
        await asyncio.sleep(0)


async def _tournament_names() -> list[str]:
    return sorted(await Tournament.objects.all().values_list("name", flat=True))


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_commit_and_its_callbacks(db_truncate):
    callback_entered = asyncio.Event()
    release_callback = asyncio.Event()
    fired: list[str] = []

    async def slow_on_commit() -> None:
        callback_entered.set()
        await release_callback.wait()
        fired.append("on_commit")

    async def body() -> None:
        async with Transactions.atomic():
            await Tournament.objects.create(name="committed")
            Transactions.on_commit(slow_on_commit)

    async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
        task = asyncio.ensure_future(body())
        await callback_entered.wait()
        for _ in range(3):
            task.cancel()
            for _ in range(5):
                await asyncio.sleep(0)
            assert not task.done(), "a repeated cancel() must not break out of the shielded COMMIT"
        release_callback.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert fired == ["on_commit"]
        assert await _tournament_names() == ["committed"]
        async with Transactions.atomic():
            await Tournament.objects.create(name="next")
        assert await _tournament_names() == ["committed", "next"]


@pytest.mark.asyncio
async def test_repeated_cancellation_of_a_shielded_call_waits_until_it_lands():
    from hare.dialects.base.client import TransactionClient

    release = asyncio.Event()
    landed: list[bool] = []

    async def operation() -> None:
        await release.wait()

    async def run_it() -> None:
        await TransactionClient._run_shielded_from_cancellation(operation(), on_landed=lambda: landed.append(True))

    async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
        task = asyncio.ensure_future(run_it())
        await asyncio.sleep(0)
        for _ in range(4):
            task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert landed == [True]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_query_hook_database_work_survives_the_rollback_of_the_observed_transaction(db_truncate):
    async def audit_hook(event) -> None:
        sql = event.sql
        if sql and "INSERT" in sql and "tournament" in sql.lower():
            await Author.objects.create(name="audit")

    Observers.observe(QueryExecuted, audit_hook)
    try:
        async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
            with pytest.raises(Boom):
                async with Transactions.atomic():
                    await Tournament.objects.create(name="rolled back")
                    await asyncio.sleep(0.01)
                    raise Boom
            await Observers.wait_for_pending()
            async with Transactions.atomic():
                await Tournament.objects.create(name="committed")
            await Observers.wait_for_pending()
    finally:
        Observers.unobserve(QueryExecuted, audit_hook)

    assert await _tournament_names() == ["committed"]
    assert await Author.objects.filter(name="audit").count() == 2


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_transaction_hook_runs_outside_the_transaction_it_observes(db_truncate):
    async def begin_hook(event) -> None:
        event = event.type
        if event is TransactionEventType.BEGIN:
            await Author.objects.create(name="begin")

    Observers.observe(TransactionEvent, begin_hook)
    try:
        async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
            with pytest.raises(Boom):
                async with Transactions.atomic():
                    await Tournament.objects.create(name="rolled back")
                    await asyncio.sleep(0.01)
                    raise Boom
            await Observers.wait_for_pending()
    finally:
        Observers.unobserve(TransactionEvent, begin_hook)

    assert await Tournament.objects.all().count() == 0
    assert await Author.objects.filter(name="begin").count() == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_commit_waits_for_a_sibling_statement_still_in_flight(db_truncate):
    async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
        async with Transactions.atomic() as connection:
            await Tournament.objects.create(name="parent")
            sibling = asyncio.ensure_future(connection.execute(_slow_query_sql(0.3)))
            await _wait_until(lambda: bool(connection._savepoint_lock._open_spans))
        # The COMMIT only landed once the sibling's statement was done.
        assert sibling.done()
        await sibling
    assert await _tournament_names() == ["parent"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_statement_started_after_commit_began_is_refused(db_truncate):
    async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
        async with Transactions.atomic() as connection:
            await Tournament.objects.create(name="parent")
            sibling = asyncio.ensure_future(connection.execute(_slow_query_sql(0.3)))
            await _wait_until(lambda: bool(connection._savepoint_lock._open_spans))
            commit = asyncio.ensure_future(connection.commit())
            await _wait_until(lambda: connection._ending)
            with pytest.raises(TransactionManagementError, match="being committed or rolled back"):
                await Tournament.objects.create(name="too late")
            await commit
            await sibling
    assert await _tournament_names() == ["parent"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_nested_wrapper_refuses_to_run_after_the_outer_transaction_committed(db_truncate):
    async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
        async with Transactions.atomic() as outer:
            async with Transactions.atomic() as inner:
                await outer.commit()
                with pytest.raises(TransactionManagementError, match="already finalised"):
                    await Tournament.objects.using(inner).create(name="via inner")
                with pytest.raises(TransactionManagementError, match="already finalised"):
                    await inner.execute("SELECT 1")
    assert await Tournament.objects.all().count() == 0


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_nested_wrapper_write_never_lands_in_another_tasks_transaction(db_truncate):
    other_opened = asyncio.Event()
    finish_other = asyncio.Event()

    async def other() -> None:
        async with Transactions.atomic():
            await Tournament.objects.create(name="other")
            other_opened.set()
            await finish_other.wait()

    async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
        async with Transactions.atomic() as outer:
            async with Transactions.atomic() as inner:
                await outer.rollback()
                other_task = Connections.current().create_task_outside_transactions(other())
                await other_opened.wait()
                with pytest.raises(TransactionManagementError, match="already finalised"):
                    await Tournament.objects.using(inner).create(name="stray")
                finish_other.set()
                await other_task
    assert await _tournament_names() == ["other"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_sibling_callbacks_survive_the_rollback_of_another_siblings_savepoint(db_truncate):
    fired: list[str] = []
    savepoint_open = asyncio.Event()
    sibling_registered = asyncio.Event()

    async def rolls_back_its_savepoint() -> None:
        with pytest.raises(Boom):
            async with Transactions.atomic():
                Transactions.on_commit(lambda: fired.append("savepoint on_commit"))
                Transactions.on_rollback(lambda: fired.append("savepoint on_rollback"))
                savepoint_open.set()
                await sibling_registered.wait()
                raise Boom

    async def registers_at_outer_level() -> None:
        await savepoint_open.wait()
        Transactions.on_commit(lambda: fired.append("sibling on_commit"))
        Transactions.on_rollback(lambda: fired.append("sibling on_rollback"))
        sibling_registered.set()

    async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
        async with Transactions.atomic():
            await asyncio.gather(rolls_back_its_savepoint(), registers_at_outer_level())
            assert fired == ["savepoint on_rollback"]
    assert fired == ["savepoint on_rollback", "sibling on_commit"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_manual_savepoint_rollback_discards_only_its_own_callbacks(db_truncate):
    fired: list[str] = []
    async with Transactions.atomic():
        Transactions.on_commit(lambda: fired.append("outer on_commit"))
        async with Transactions.atomic() as inner:
            Transactions.on_commit(lambda: fired.append("inner on_commit"))
            Transactions.on_rollback(lambda: fired.append("inner on_rollback"))
            await inner.rollback()
        assert fired == ["inner on_rollback"]
    assert fired == ["inner on_rollback", "outer on_commit"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_on_commit_after_a_manual_commit_runs_immediately(db_truncate):
    fired: list[str] = []
    async with Transactions.atomic() as connection:
        await Tournament.objects.create(name="committed")
        await connection.commit()
        Transactions.on_commit(lambda: fired.append("after commit"))
        assert fired == ["after commit"]
    assert fired == ["after commit"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_on_commit_from_a_task_that_outlived_its_transaction_runs_immediately(db_truncate):
    fired: list[str] = []
    transaction_ended = asyncio.Event()

    async def outliving_task() -> None:
        await transaction_ended.wait()
        Transactions.on_commit(lambda: fired.append("outliving task"))

    async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
        async with Transactions.atomic():
            task = asyncio.ensure_future(outliving_task())
        transaction_ended.set()
        await task
    assert fired == ["outliving task"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_on_rollback_after_the_transaction_ended_is_never_run(db_truncate, caplog):
    fired: list[str] = []
    with caplog.at_level(logging.WARNING, logger="hare"):
        async with Transactions.atomic() as connection:
            await connection.rollback()
            Transactions.on_rollback(lambda: fired.append("after rollback"))
    assert fired == []
    assert "on_rollback() called after the transaction" in caplog.text


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_top_level_context_exited_from_another_task(db_truncate):
    context = Transactions.atomic()

    async def enter() -> None:
        await context.__aenter__()
        await Tournament.objects.create(name="entered")

    async def leave() -> None:
        await context.__aexit__(None, None, None)

    async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
        await asyncio.ensure_future(enter())
        await asyncio.ensure_future(leave())
        async with Transactions.atomic():
            await Tournament.objects.create(name="next")
    assert await _tournament_names() == ["entered", "next"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_nested_context_exited_from_another_task_does_not_wedge_the_outer_one(db_truncate):
    async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
        async with Transactions.atomic() as outer:
            nested_context = outer._in_transaction()

            async def enter() -> None:
                await nested_context.__aenter__()
                await Tournament.objects.create(name="nested")

            async def leave() -> None:
                await nested_context.__aexit__(None, None, None)

            await asyncio.ensure_future(enter())
            await asyncio.ensure_future(leave())
            assert outer._savepoint_lock._open_spans == []
            await Tournament.objects.create(name="outer")
    assert await _tournament_names() == ["nested", "outer"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_failing_savepoint_on_rollback_callback_is_not_reported_as_a_failed_rollback(db_truncate, caplog):
    def failing_callback() -> None:
        raise ValueError("callback failed")

    with caplog.at_level(logging.ERROR):
        async with Transactions.atomic():
            await Tournament.objects.create(name="outer")
            with pytest.raises(Boom) as raised:
                async with Transactions.atomic():
                    Transactions.on_rollback(failing_callback)
                    await Tournament.objects.create(name="inner")
                    raise Boom
    notes = getattr(raised.value, "__notes__", [])
    assert not any("Rolling back the savepoint also failed" in note for note in notes)
    assert any("on_rollback() callback failed after rolling back the savepoint" in note for note in notes)
    assert "Rolling back the savepoint failed" not in caplog.text
    assert await _tournament_names() == ["outer"]


@requires_features(dialect="postgresql")
@pytest.mark.parametrize("block_raises", [True, False])
@pytest.mark.asyncio
async def test_lost_connection_still_ends_the_transaction_as_a_rollback(db_truncate, block_raises):
    events: list[tuple[TransactionEvent, bool]] = []
    fired: list[str] = []

    def hook(event) -> None:
        event, exception = event.type, event.error
        events.append((event, exception is not None))

    Observers.observe(TransactionEvent, hook)
    try:
        async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
            with pytest.raises((DBConnectionError, Boom)):
                async with Transactions.atomic() as connection:
                    Transactions.on_commit(lambda: fired.append("on_commit"))
                    Transactions.on_rollback(lambda: fired.append("on_rollback"))
                    _, rows = await connection.execute("SELECT pg_backend_pid() AS pid")
                    backend_pid = rows[0]["pid"]
                    await connection.get_non_transactional_client().execute(
                        "SELECT pg_terminate_backend($1)", [backend_pid]
                    )
                    try:
                        await _wait_for_connection_loss(connection)
                    except DBConnectionError:
                        if block_raises:
                            raise Boom from None
            await Observers.wait_for_pending()
    finally:
        Observers.unobserve(TransactionEvent, hook)

    assert [event for event, _ in events] == [TransactionEventType.BEGIN, TransactionEventType.ROLLBACK]
    assert fired == ["on_rollback"]
    async with Transactions.atomic():
        await Tournament.objects.create(name="after")
    assert await _tournament_names() == ["after"]


async def _wait_for_connection_loss(connection) -> None:
    """Runs statements on ``connection`` until the terminated backend makes one fail."""
    for _ in range(200):
        await connection.execute("SELECT 1")
        await asyncio.sleep(0.01)
    raise AssertionError("the terminated backend never closed the connection")


@requires_features(dialect="postgresql")
@pytest.mark.parametrize(
    "scenario", ["commit", "rollback", "savepoint_release", "savepoint_rollback", "non_transactional"]
)
@pytest.mark.asyncio
async def test_connection_lost_mid_use_still_frees_its_pool_slot(db_truncate, scenario):
    """A pooled connection the server closed while it was checked out went back to asyncpg's pool
    without freeing its slot - closing the pool afterwards waited out its whole timeout."""
    shared_client = Connections.get("models")
    client = type(shared_client)(
        connection_name="lost_connection_pool_slot_test",
        user=shared_client.user,
        password=shared_client.password,
        database=shared_client.database,
        host=shared_client.host,
        port=shared_client.port,
    )
    await client.create_connection(with_db=True)
    try:
        async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
            if scenario == "non_transactional":
                await _lose_the_connection_of_a_running_query(client)
            else:
                await _lose_the_connection_inside_a_transaction(client, scenario)
        _, rows = await client.execute("SELECT 1 AS alive")
        assert rows[0]["alive"] == 1
    finally:
        close_started_at = time.monotonic()
        await client.close()
        close_seconds = time.monotonic() - close_started_at
    assert close_seconds < POOL_CLOSE_TIME_LIMIT_SECONDS


async def _lose_the_connection_inside_a_transaction(client, scenario: str) -> None:
    """Kills the backend of a transaction (or of a savepoint inside one) opened on ``client``."""
    block_raises = scenario in ("rollback", "savepoint_rollback")
    with pytest.raises((DBConnectionError, Boom)):
        async with client._in_transaction() as transaction_client:
            if scenario.startswith("savepoint"):
                async with transaction_client._in_transaction() as savepoint_client:
                    await _kill_the_backend_of(client, savepoint_client, block_raises)
            else:
                await _kill_the_backend_of(client, transaction_client, block_raises)


async def _kill_the_backend_of(client, victim_client, block_raises: bool) -> None:
    """Terminates ``victim_client``'s backend from another pooled connection of ``client`` and
    waits until ``victim_client`` notices."""
    _, rows = await victim_client.execute("SELECT pg_backend_pid() AS pid")
    await client.execute("SELECT pg_terminate_backend($1)", [rows[0]["pid"]])
    try:
        await _wait_for_connection_loss(victim_client)
    except DBConnectionError:
        if block_raises:
            raise Boom from None


async def _lose_the_connection_of_a_running_query(client) -> None:
    """Terminates the backend of a plain (non-transactional) query while it runs on ``client``."""
    victim_task = asyncio.create_task(client.execute("SELECT pg_sleep(30) AS lost_connection_victim"))
    for _ in range(500):
        _, rows = await client.execute(
            "SELECT count(pg_terminate_backend(pid)) AS killed FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
            "AND query LIKE '%lost_connection_victim%'"
        )
        if rows[0]["killed"]:
            break
        await asyncio.sleep(0.01)
    with pytest.raises(DBConnectionError):
        await victim_task
