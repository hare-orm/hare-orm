import asyncio
import time

import pytest

from hare.contrib.test import requires_features
from hare.core.connections import Connections
from hare.dialects.base.client import TransactionClient
from hare.exceptions import IntegrityError, TransactionManagementError
from hare.transactions.transactions import Transactions
from tests.testmodels import CharPkModel, Tournament


class Boom(Exception):
    """A very specific exception so as to not accidentally catch another exception."""


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_transaction_management_error_from_block_still_rolls_back(db_truncate):
    fired = []
    with pytest.raises(TransactionManagementError):
        async with Transactions.atomic():
            Transactions.on_rollback(lambda: fired.append("rolled-back"))
            await Tournament.objects.create(name="not-persisted")
            raise TransactionManagementError("raised by the block itself")

    assert fired == ["rolled-back"]
    assert await Tournament.objects.all().count() == 0
    async with Transactions.atomic():
        await Tournament.objects.create(name="persisted")
    assert await Tournament.objects.all().count() == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_transaction_management_error_from_nested_block_rolls_back_its_savepoint(db_truncate):
    fired = []
    async with Transactions.atomic():
        await Tournament.objects.create(name="outer")
        with pytest.raises(TransactionManagementError):
            async with Transactions.atomic():
                Transactions.on_rollback(lambda: fired.append("savepoint-rolled-back"))
                await Tournament.objects.create(name="inner")
                raise TransactionManagementError("raised by the nested block itself")

    assert fired == ["savepoint-rolled-back"]
    assert [tournament.name for tournament in await Tournament.objects.all()] == ["outer"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_aborted_transaction_error_still_rolls_back_and_runs_rollback_callbacks(db_truncate):
    fired = []
    with pytest.raises(TransactionManagementError):
        async with Transactions.atomic():
            Transactions.on_rollback(lambda: fired.append("rolled-back"))
            await CharPkModel.objects.create(id="duplicate")
            with pytest.raises(IntegrityError):
                await CharPkModel.objects.create(id="duplicate")
            await Tournament.objects.all().count()

    assert fired == ["rolled-back"]
    assert await CharPkModel.objects.all().count() == 0


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_aborted_transaction_error_leaves_no_stale_state_on_rolled_back_savepoint(db_truncate):
    async with Transactions.atomic():
        await Tournament.objects.create(name="outer")
        with pytest.raises(TransactionManagementError):
            async with Transactions.atomic():
                await CharPkModel.objects.create(id="duplicate")
                with pytest.raises(IntegrityError):
                    await CharPkModel.objects.create(id="duplicate")
                await Tournament.objects.all().count()
        assert await CharPkModel.objects.all().count() == 0

    assert await Tournament.objects.all().count() == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_failed_commit_rolls_back_and_releases_the_connection(db_truncate):
    database = Connections.get("models")
    await database.execute_script("CREATE TABLE zz_parent (id INTEGER PRIMARY KEY)")
    await database.execute_script(
        "CREATE TABLE zz_child (parent_id INTEGER REFERENCES zz_parent(id) DEFERRABLE INITIALLY DEFERRED)"
    )
    fired = []

    with pytest.raises(IntegrityError):
        async with Transactions.atomic() as connection:
            Transactions.on_rollback(lambda: fired.append("rolled-back"))
            await Tournament.objects.create(name="not-persisted")
            await connection.execute("INSERT INTO zz_child VALUES (999)")

    assert fired == ["rolled-back"]
    assert await Tournament.objects.all().count() == 0
    async with Transactions.atomic():
        await Tournament.objects.create(name="persisted")
    assert await Tournament.objects.all().count() == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_release_failure_does_not_mask_block_exception_or_leak_ambient_connection(db_truncate):
    child_outcome = []

    async def child():
        try:
            await Tournament.objects.create(name="child")
            child_outcome.append("created")
        except Exception as error:
            child_outcome.append(type(error).__name__)

    child_task = None
    with pytest.raises(Boom):
        async with Transactions.atomic():
            child_task = asyncio.create_task(child())
            await asyncio.sleep(0)
            raise Boom

    assert child_task is not None
    await child_task
    assert not isinstance(Connections.current().get("models"), TransactionClient)
    await Tournament.objects.all().count()
    async with Transactions.atomic():
        await Tournament.objects.create(name="after")


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_reentering_used_transaction_context_raises_before_any_begin(db_truncate):
    transaction_context = Transactions.atomic()
    async with transaction_context:
        await Tournament.objects.create(name="first")

    with pytest.raises(TransactionManagementError):
        async with transaction_context:
            pytest.fail("the block of a reused transaction context must never run")

    assert not isinstance(Connections.current().get("models"), TransactionClient)
    async with Transactions.atomic():
        await Tournament.objects.create(name="second")
    assert await Tournament.objects.all().count() == 2


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_reentering_used_nested_transaction_context_raises_before_any_savepoint(db_truncate):
    async with Transactions.atomic():
        nested_context = Transactions.atomic()
        async with nested_context:
            await Tournament.objects.create(name="first")

        with pytest.raises(TransactionManagementError):
            async with nested_context:
                pytest.fail("the block of a reused transaction context must never run")

        await Tournament.objects.create(name="second")
    assert await Tournament.objects.all().count() == 2


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_task_outliving_its_nested_block_queries_at_the_outer_level(db_truncate):
    nested_block_closed = asyncio.Event()

    async def count_later():
        await nested_block_closed.wait()
        return await Tournament.objects.all().count()

    async with Transactions.atomic():
        await Tournament.objects.create(name="outer")
        async with Transactions.atomic():
            await Tournament.objects.create(name="inner")
            later_task = asyncio.create_task(count_later())
        nested_block_closed.set()
        assert await asyncio.wait_for(later_task, timeout=5) == 2


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_task_outliving_two_nested_levels_can_open_its_own_nested_transaction(db_truncate):
    nested_blocks_closed = asyncio.Event()

    async def create_later():
        await nested_blocks_closed.wait()
        async with Transactions.atomic():
            await Tournament.objects.create(name="from-task")

    async with Transactions.atomic():
        async with Transactions.atomic():
            async with Transactions.atomic():
                later_task = asyncio.create_task(create_later())
        nested_blocks_closed.set()
        await asyncio.wait_for(later_task, timeout=5)
        assert await Tournament.objects.all().count() == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_on_commit_callback_can_query_and_open_transactions(db_truncate):
    outcomes = []

    async def query_in_callback():
        await Tournament.objects.create(name="from-query")
        outcomes.append("query")

    async def transaction_in_callback():
        async with Transactions.atomic():
            await Tournament.objects.create(name="from-transaction")
        outcomes.append("transaction")

    async with Transactions.atomic():
        Transactions.on_commit(query_in_callback)
        Transactions.on_commit(transaction_in_callback)

    assert outcomes == ["query", "transaction"]
    assert sorted(tournament.name for tournament in await Tournament.objects.all()) == [
        "from-query",
        "from-transaction",
    ]
    assert not isinstance(Connections.current().get("models"), TransactionClient)


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_on_rollback_callback_can_query_and_open_transactions(db_truncate):
    async def compensate():
        async with Transactions.atomic():
            await Tournament.objects.create(name="compensation")

    with pytest.raises(Boom):
        async with Transactions.atomic():
            await Tournament.objects.create(name="rolled-back")
            Transactions.on_rollback(compensate)
            raise Boom

    assert [tournament.name for tournament in await Tournament.objects.all()] == ["compensation"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_on_commit_registered_from_a_callback_runs_immediately(db_truncate):
    order = []

    def outer_callback():
        order.append("outer-start")
        Transactions.on_commit(lambda: order.append("nested"))
        order.append("outer-end")

    async with Transactions.atomic():
        Transactions.on_commit(outer_callback)

    assert order == ["outer-start", "nested", "outer-end"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_on_commit_callback_that_opens_transaction_leaves_no_open_transaction(db_truncate):
    async def transaction_in_callback():
        async with Transactions.atomic():
            await Tournament.objects.create(name="from-callback")

    async with Transactions.atomic():
        Transactions.on_commit(transaction_in_callback)

    async with Transactions.atomic():
        await Tournament.objects.create(name="after")
    assert await Tournament.objects.all().count() == 2


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_failing_rollback_callback_does_not_replace_the_block_exception(db_truncate):
    def failing_callback():
        raise RuntimeError("callback failed")

    with pytest.raises(Boom) as raised:
        async with Transactions.atomic():
            await Tournament.objects.create(name="rolled-back")
            Transactions.on_rollback(failing_callback)
            raise Boom("original")

    assert str(raised.value) == "original"
    assert any("callback failed" in note for note in raised.value.__notes__)
    assert await Tournament.objects.all().count() == 0


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_failing_commit_callback_without_block_exception_is_still_raised(db_truncate):
    def failing_callback():
        raise RuntimeError("callback failed")

    with pytest.raises(RuntimeError, match="callback failed"):
        async with Transactions.atomic():
            await Tournament.objects.create(name="committed")
            Transactions.on_commit(failing_callback)

    assert await Tournament.objects.all().count() == 1


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_waiting_for_a_pool_slot_in_a_transaction_honours_timeout_and_cancellation(db_truncate):
    pool_size = Connections.get("models").pool_maxsize

    async def hold_pool_full_of_transactions(release_holders: asyncio.Event) -> list[asyncio.Task]:
        opened = 0
        all_opened = asyncio.Event()

        async def hold_transaction() -> None:
            nonlocal opened
            async with Transactions.atomic():
                opened += 1
                if opened == pool_size:
                    all_opened.set()
                await release_holders.wait()

        holders = [asyncio.create_task(hold_transaction()) for _ in range(pool_size)]
        await asyncio.wait_for(all_opened.wait(), timeout=15)
        return holders

    async def wait_for_a_slot() -> None:
        async with Transactions.atomic():
            pass

    release_holders = asyncio.Event()
    holders = await hold_pool_full_of_transactions(release_holders)
    try:
        started_at = time.monotonic()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(wait_for_a_slot(), timeout=0.5)
        assert time.monotonic() - started_at < 3

        waiting_task = asyncio.create_task(wait_for_a_slot())
        await asyncio.sleep(0.2)
        started_at = time.monotonic()
        waiting_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting_task
        assert time.monotonic() - started_at < 3
    finally:
        release_holders.set()
        await asyncio.gather(*holders)

    release_holders = asyncio.Event()
    holders = await hold_pool_full_of_transactions(release_holders)
    release_holders.set()
    await asyncio.gather(*holders)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_transaction_finished_error_from_a_callback_is_not_swallowed(db_truncate):
    if "hare.dialects.postgresql.drivers.rust_pg" not in type(db_truncate.db()).__module__:
        pytest.skip("Only rust_pg reconciles pg.TransactionFinishedError")
    from rust.native import pg

    def failing_callback():
        raise pg.TransactionFinishedError("raised by the callback itself")

    with pytest.raises(TransactionManagementError) as commit_error:
        async with Transactions.atomic():
            Transactions.on_commit(failing_callback)
    assert isinstance(commit_error.value.__context__, pg.TransactionFinishedError)

    with pytest.raises(Boom):
        async with Transactions.atomic():
            Transactions.on_rollback(failing_callback)
            raise Boom

    async with Transactions.atomic():
        await Tournament.objects.create(name="after")


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_commit_of_a_transaction_aborted_by_a_caught_error_raises_and_rolls_back(db_truncate):
    fired = []
    with pytest.raises(TransactionManagementError, match="current transaction is aborted"):
        async with Transactions.atomic():
            Transactions.on_commit(lambda: fired.append("committed"))
            Transactions.on_rollback(lambda: fired.append("rolled-back"))
            await CharPkModel.objects.create(id="duplicate")
            with pytest.raises(IntegrityError):
                await CharPkModel.objects.create(id="duplicate")

    assert fired == ["rolled-back"]
    assert await CharPkModel.objects.all().count() == 0
    async with Transactions.atomic():
        await CharPkModel.objects.create(id="usable-afterwards")
    assert await CharPkModel.objects.all().count() == 1


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_commit_of_a_transaction_aborted_by_a_timed_out_statement_raises(db_truncate):
    with pytest.raises(TransactionManagementError, match="current transaction is aborted"):
        async with Transactions.atomic() as connection:
            await Tournament.objects.create(name="not-persisted")
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.2):
                    await connection.execute("SELECT pg_sleep(2)")

    assert await Tournament.objects.all().count() == 0


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_commit_after_a_statement_cancelled_before_it_ran_raises(db_truncate):
    """A statement cancelled client-side may or may not have run on the server - the transaction
    must not commit either way, even when Postgres never saw the statement fail."""
    with pytest.raises(TransactionManagementError, match="current transaction is aborted"):
        async with Transactions.atomic() as connection:
            await Tournament.objects.create(name="not-persisted")
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0):
                    await connection.execute('INSERT INTO "tournament" ("name") VALUES ($1)', ["cancelled"])

    assert await Tournament.objects.all().count() == 0


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_statement_cancelled_inside_a_rolled_back_savepoint_does_not_block_the_commit(db_truncate):
    async with Transactions.atomic():
        await Tournament.objects.create(name="outer")
        with pytest.raises(TimeoutError):
            async with Transactions.atomic() as savepoint_connection:
                async with asyncio.timeout(0):
                    await savepoint_connection.execute("SELECT pg_sleep(2)")

    assert [tournament.name for tournament in await Tournament.objects.all()] == ["outer"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_statement_cancelled_inside_a_released_savepoint_blocks_the_commit(db_truncate):
    with pytest.raises(TransactionManagementError, match="current transaction is aborted"):
        async with Transactions.atomic():
            await Tournament.objects.create(name="not-persisted")
            async with Transactions.atomic() as savepoint_connection:
                with pytest.raises(TimeoutError):
                    async with asyncio.timeout(0):
                        await savepoint_connection.execute("SELECT pg_sleep(2)")

    assert await Tournament.objects.all().count() == 0


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_error_rolled_back_with_its_savepoint_does_not_block_the_commit(db_truncate):
    async with Transactions.atomic():
        await Tournament.objects.create(name="outer")
        with pytest.raises(IntegrityError):
            async with Transactions.atomic():
                await CharPkModel.objects.create(id="duplicate")
                await CharPkModel.objects.create(id="duplicate")

    assert [tournament.name for tournament in await Tournament.objects.all()] == ["outer"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_failed_savepoint_release_rolls_back_to_the_savepoint(db_truncate):
    fired = []
    async with Transactions.atomic():
        await Tournament.objects.create(name="outer")
        with pytest.raises(TransactionManagementError):
            async with Transactions.atomic():
                Transactions.on_commit(lambda: fired.append("inner committed"))
                Transactions.on_rollback(lambda: fired.append("inner rolled back"))
                await CharPkModel.objects.create(id="duplicate")
                with pytest.raises(IntegrityError):
                    await CharPkModel.objects.create(id="duplicate")
        assert await Tournament.objects.all().count() == 1

    assert fired == ["inner rolled back"]
    assert await Tournament.objects.all().count() == 1
    assert await CharPkModel.objects.all().count() == 0


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_caught_integrity_error_does_not_abort_a_sqlite_transaction(db_truncate):
    fired = []
    async with Transactions.atomic():
        Transactions.on_commit(lambda: fired.append("committed"))
        await CharPkModel.objects.create(id="duplicate")
        with pytest.raises(IntegrityError):
            await CharPkModel.objects.create(id="duplicate")

    assert fired == ["committed"]
    assert await CharPkModel.objects.all().count() == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_failing_rollback_callback_does_not_replace_the_nested_block_exception(db_truncate):
    def failing_callback():
        raise RuntimeError("callback failed")

    async with Transactions.atomic():
        await Tournament.objects.create(name="outer")
        with pytest.raises(Boom) as raised:
            async with Transactions.atomic():
                await Tournament.objects.create(name="rolled-back")
                Transactions.on_rollback(failing_callback)
                raise Boom("original")

    assert str(raised.value) == "original"
    assert any("callback failed" in note for note in raised.value.__notes__)
    assert [tournament.name for tournament in await Tournament.objects.all()] == ["outer"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_execute_script_inside_a_transaction_rolls_back_with_it(db_truncate):
    with pytest.raises(Boom):
        async with Transactions.atomic() as connection:
            await connection.execute_script(
                'INSERT INTO "tournament" ("id", "name", "created") VALUES (1, \'a;b\', \'2024-01-01\');'
                'INSERT INTO "tournament" ("id", "name", "created") VALUES (2, \'c\', \'2024-01-01\')'
            )
            raise Boom("undo the script")

    assert await Tournament.objects.all().count() == 0


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_waiting_for_a_pool_connection_busy_with_plain_queries_honours_timeout(db_truncate):
    """On rust_pg the wait for a pooled connection used to happen inside the shielded BEGIN, so a
    pool held by plain (non-transaction) queries ignored wait_for()/cancellation until one ended."""
    client = Connections.get("models")
    pool_size = client.pool_maxsize
    sleepers = [asyncio.create_task(client.execute("SELECT pg_sleep(2)")) for _ in range(pool_size)]
    try:
        await asyncio.sleep(0.3)

        async def open_transaction() -> None:
            async with Transactions.atomic():
                pass

        started_at = time.monotonic()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(open_transaction(), timeout=0.3)
        assert time.monotonic() - started_at < 1.2
    finally:
        await asyncio.wait_for(asyncio.gather(*sleepers), 15)

    async with Transactions.atomic():
        await Tournament.objects.create(name="pool still healthy")
    assert await Tournament.objects.filter(name="pool still healthy").count() == 1


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_stream_and_sibling_task_queries_share_one_transaction(db_truncate):
    """asyncpg's stream() fetched from its cursor without the transaction's connection lock, so a
    sibling task's query on the same transaction collided with a fetch: "another operation is in
    progress"."""
    await Tournament.objects.bulk_create([Tournament(name=f"t{index}") for index in range(60)])

    async def consume_stream() -> int:
        streamed = 0
        async for _ in Tournament.objects.all().order_by("id").stream(chunk_size=2):
            streamed += 1
            await asyncio.sleep(0)
        return streamed

    async def run_sibling_queries() -> int:
        total = 0
        for _ in range(30):
            total += await Tournament.objects.filter(name="t1").count()
            await asyncio.sleep(0)
        return total

    async with Transactions.atomic():
        streamed, counted = await asyncio.wait_for(asyncio.gather(consume_stream(), run_sibling_queries()), 20)
        nested = 0
        async for tournament in Tournament.objects.all().order_by("id").stream(chunk_size=5):
            nested += await Tournament.objects.filter(id=tournament.id).count()
            if nested == 3:
                break
    assert (streamed, counted, nested) == (60, 30, 3)
    assert await asyncio.wait_for(Tournament.objects.all().count(), 5) == 60
