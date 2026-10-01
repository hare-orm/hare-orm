import asyncio
import os
import sqlite3
import time
import types
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from hare import Connections
from hare.contrib.test import requires_features
from hare.core.context import HareContext
from hare.dialects.base.client import TransactionClient
from hare.dialects.base.db_url import DbUrlConfigGenerator
from hare.dialects.base.nested_savepoint_lock import NestedSavepointLock
from hare.exceptions import OperationalError, QueryError, TransactionManagementError
from hare.transactions.transactions import Transactions
from tests.testmodels import CharPkModel, Event, Team, Tournament
from tests.utils.database_under_test import DatabaseUnderTest
from tests.utils.multi_database_context import MultiDatabaseTestContext


class SomeException(Exception):
    """
    A very specific exception so as to not accidentally catch another exception.
    """


@Transactions.atomic()
async def atomic_decorated_func():
    tournament = Tournament(name="Test")
    await tournament.save()
    return tournament


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_transactions(db_truncate):
    """Test basic transaction rollback on exception."""
    with pytest.raises(SomeException):
        async with Transactions.atomic():
            tournament = Tournament(name="Test")
            await tournament.save()
            await Tournament.objects.filter(id=tournament.id).update(name="Updated name")
            saved_event = await Tournament.objects.filter(name="Updated name").first()
            assert saved_event.id == tournament.id
            raise SomeException("Some error")

    saved_event = await Tournament.objects.filter(name="Updated name").first()
    assert saved_event is None


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_get_or_create_transaction_using_db(db_truncate):
    """Test get_or_create with explicit connection rollback."""
    async with Transactions.atomic() as connection:
        obj = await CharPkModel.objects.using(connection).get_or_create(id="FooMip")
        assert obj is not None
        await connection.rollback()

    obj2 = await CharPkModel.objects.filter(id="FooMip").first()
    assert obj2 is None


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_consequent_nested_transactions(db_truncate):
    """Test consequent nested transactions."""
    async with Transactions.atomic():
        await Tournament.objects.create(name="Test")
        async with Transactions.atomic():
            await Tournament.objects.create(name="Nested 1")
        await Tournament.objects.create(name="Test 2")
        async with Transactions.atomic():
            await Tournament.objects.create(name="Nested 2")

    assert set(await Tournament.objects.all().values_list("name", flat=True)) == {
        "Test",
        "Nested 1",
        "Test 2",
        "Nested 2",
    }


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_caught_exception_in_nested_transaction(db_truncate):
    """Test that caught exception in nested transaction only rolls back inner."""
    async with Transactions.atomic():
        tournament = await Tournament.objects.create(name="Test")
        await Tournament.objects.filter(id=tournament.id).update(name="Updated name")
        saved_event = await Tournament.objects.filter(name="Updated name").first()
        assert saved_event.id == tournament.id
        with pytest.raises(SomeException):
            async with Transactions.atomic():
                tournament = await Tournament.objects.create(name="Nested")
                saved_tournament = await Tournament.objects.filter(name="Nested").first()
                assert tournament.id == saved_tournament.id
                raise SomeException("Some error")

    saved_event = await Tournament.objects.filter(name="Updated name").first()
    assert saved_event is not None
    not_saved_event = await Tournament.objects.filter(name="Nested").first()
    assert not_saved_event is None


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_nested_tx_do_not_commit(db_truncate):
    """Test that nested transactions don't commit if outer fails."""
    with pytest.raises(SomeException):
        async with Transactions.atomic():
            tournament = await Tournament.objects.create(name="Test")
            async with Transactions.atomic():
                tournament.name = "Nested"
                await tournament.save()

            raise SomeException("Some error")

    assert await Tournament.objects.filter(id=tournament.id).count() == 0


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_nested_rollback_does_not_enable_autocommit(db_truncate):
    """Test that nested rollback doesn't enable autocommit."""
    with pytest.raises(SomeException, match="Error 2"):
        async with Transactions.atomic():
            await Tournament.objects.create(name="Test1")
            with pytest.raises(SomeException, match="Error 1"):
                async with Transactions.atomic():
                    await Tournament.objects.create(name="Test2")
                    raise SomeException("Error 1")

            await Tournament.objects.create(name="Test3")
            raise SomeException("Error 2")

    assert await Tournament.objects.all().count() == 0


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_nested_savepoint_rollbacks(db_truncate):
    """Test nested savepoint rollbacks."""
    async with Transactions.atomic():
        await Tournament.objects.create(name="Outer Transaction 1")

        with pytest.raises(SomeException, match="Inner 1"):
            async with Transactions.atomic():
                await Tournament.objects.create(name="Inner 1")
                raise SomeException("Inner 1")

        await Tournament.objects.create(name="Outer Transaction 2")

        with pytest.raises(SomeException, match="Inner 2"):
            async with Transactions.atomic():
                await Tournament.objects.create(name="Inner 2")
                raise SomeException("Inner 2")

        await Tournament.objects.create(name="Outer Transaction 3")

    assert await Tournament.objects.all().values_list("name", flat=True) == [
        "Outer Transaction 1",
        "Outer Transaction 2",
        "Outer Transaction 3",
    ]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_nested_savepoint_rollback_but_other_succeed(db_truncate):
    """Test nested savepoint rollback while other nested transactions succeed."""
    async with Transactions.atomic():
        await Tournament.objects.create(name="Outer Transaction 1")

        with pytest.raises(SomeException, match="Inner 1"):
            async with Transactions.atomic():
                await Tournament.objects.create(name="Inner 1")
                raise SomeException("Inner 1")

        await Tournament.objects.create(name="Outer Transaction 2")

        async with Transactions.atomic():
            await Tournament.objects.create(name="Inner 2")

        await Tournament.objects.create(name="Outer Transaction 3")

    assert await Tournament.objects.all().values_list("name", flat=True) == [
        "Outer Transaction 1",
        "Outer Transaction 2",
        "Inner 2",
        "Outer Transaction 3",
    ]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_three_nested_transactions(db_truncate):
    """Test three levels of nested transactions."""
    async with Transactions.atomic():
        tournament1 = await Tournament.objects.create(name="Test")
        async with Transactions.atomic():
            tournament2 = await Tournament.objects.create(name="Nested")
            async with Transactions.atomic():
                tournament3 = await Tournament.objects.create(name="Nested2")

    assert await Tournament.objects.filter(id__in=[tournament1.id, tournament2.id, tournament3.id]).count() == 3


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_concurrent_sibling_nested_transactions(db_truncate):
    """Two sibling nested transactions (concurrent tasks each opening their own
    `outer._in_transaction()` off the SAME outer transaction) share the one physical
    connection - each got its own wrapper object with no lock at all serializing their
    SAVEPOINT/RELEASE/ROLLBACK TO statements against each other. Worse than a plain
    interleaving race: SQL's nested-savepoint model requires strict LIFO close ordering - RELEASE
    releases the named savepoint AND every savepoint opened after it - so two savepoints opened
    concurrently and closed out of that order corrupt the stack regardless of any per-statement
    locking. Reproduced directly against sqlite (RELEASE of an earlier sibling's savepoint
    silently released a later sibling's too, then the later sibling's own release failed with
    "no such savepoint")."""

    async def worker(name: str, delay: float) -> None:
        async with Transactions.atomic("models") as inner:
            await Tournament.objects.using(inner).create(name=name)
            await asyncio.sleep(delay)

    async with Transactions.atomic("models") as outer:
        await asyncio.gather(*[worker(f"Sibling{i}", 0.001 * (i % 3)) for i in range(10)])
        # The outer transaction itself must still be usable after every sibling completes.
        await Tournament.objects.using(outer).create(name="AfterSiblings")

    names = set(await Tournament.objects.all().values_list("name", flat=True))
    assert names == {f"Sibling{i}" for i in range(10)} | {"AfterSiblings"}


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_nested_gather_under_still_open_parent_does_not_deadlock(db_truncate):
    """outer -> middle -> gather(child_a, child_b), where child_a/child_b each open their OWN
    nested transaction off `middle` (not off `outer`) - a genuine deadlock under the previous
    task-identity-keyed lock: the task running `middle`'s own body holds that lock for the whole
    span it's open, then suspends on `await gather(...)`; child_a/child_b are separate
    asyncio.Task objects (gather() always schedules new ones) that need the SAME lock to nest
    under `middle`, so they block on it - which only releases once `middle`'s own task reaches
    its __aexit__, which can't happen until the very gather() it's blocked on returns. Circular
    wait, confirmed to hang indefinitely before this fix. Wrapped in wait_for so a regression
    fails fast with a clear TimeoutError instead of hanging the whole test run."""

    async def child(middle, name: str) -> None:
        async with middle._in_transaction() as inner:
            await Tournament.objects.using(inner).create(name=name)

    async with Transactions.atomic("models") as outer:
        async with outer._in_transaction() as middle:
            await asyncio.wait_for(asyncio.gather(child(middle, "A"), child(middle, "B")), timeout=5)
            await Tournament.objects.using(middle).create(name="MiddleRow")
        await Tournament.objects.using(outer).create(name="OuterRow")

    names = set(await Tournament.objects.all().values_list("name", flat=True))
    assert names == {"A", "B", "MiddleRow", "OuterRow"}


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_nested_gather_children_serialize_against_each_other_not_just_avoid_deadlock(db_truncate):
    """Avoiding the deadlock isn't enough on its own - child_a/child_b nesting under the SAME
    still-open `middle` must still fully serialize against EACH OTHER (the original
    sibling-corruption hazard test_concurrent_sibling_nested_transactions covers at the top
    level), not just against `middle`. Uses enough siblings and a real await-point (sleep) inside
    each one's span that an interleaving bug would very likely surface as either a corrupted
    savepoint ("no such savepoint") or missing rows, not just get lucky with scheduling."""

    async def child(middle, name: str, delay: float) -> None:
        async with middle._in_transaction() as inner:
            await Tournament.objects.using(inner).create(name=name)
            await asyncio.sleep(delay)

    async with Transactions.atomic("models") as outer:
        async with outer._in_transaction() as middle:
            await asyncio.wait_for(
                asyncio.gather(*[child(middle, f"Child{i}", 0.001 * (i % 3)) for i in range(10)]),
                timeout=5,
            )
            await Tournament.objects.using(middle).create(name="MiddleRow")

    names = set(await Tournament.objects.all().values_list("name", flat=True))
    assert names == {f"Child{i}" for i in range(10)} | {"MiddleRow"}


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_nested_gather_sibling_rollback_does_not_corrupt_the_other(db_truncate):
    """One of two siblings nested under the same still-open `middle` raises and rolls back - the
    OTHER sibling's own commit (release_savepoint) must still succeed and its data must survive,
    proving the savepoint stack stays correctly ordered even when one branch closes via rollback
    instead of the happy path."""

    class SiblingFailure(Exception):
        pass

    async def good_child(middle) -> None:
        async with middle._in_transaction() as inner:
            await Tournament.objects.using(inner).create(name="Survives")

    async def bad_child(middle) -> None:
        async with middle._in_transaction() as inner:
            await Tournament.objects.using(inner).create(name="RolledBack")
            raise SiblingFailure("simulated failure inside a nested sibling")

    async with Transactions.atomic("models") as outer:
        async with outer._in_transaction() as middle:
            results = await asyncio.wait_for(
                asyncio.gather(good_child(middle), bad_child(middle), return_exceptions=True),
                timeout=5,
            )
            assert results[0] is None
            assert isinstance(results[1], SiblingFailure)
            await Tournament.objects.using(middle).create(name="MiddleRow")

    names = set(await Tournament.objects.all().values_list("name", flat=True))
    assert names == {"Survives", "MiddleRow"}
    assert "RolledBack" not in names


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_ambient_write_survives_a_gather_siblings_own_savepoint_rollback(db_truncate):
    """An "ambient" write (no using= of its own - resolves through the outer transaction
    directly) issued from one asyncio.gather() sibling used to be silently discarded by a
    DIFFERENT sibling's own nested-savepoint rollback: ordinary query execution
    (ConnectionWrapper/self._lock, a fresh asyncio.Lock() per wrapper instance) was never
    synchronized against NestedSavepointLock at all - only SAVEPOINT/RELEASE SAVEPOINT/
    ROLLBACK TO calls were - so nothing stopped this write from landing on the wire BETWEEN a
    sibling's SAVEPOINT and its later ROLLBACK TO, which discarded it too even though it was
    never logically inside that sibling's own nested block and its own code path never raised.

    Runs on all 3 backends: sqlite/asyncpg via ConnectionWrapper's own span-acquire, rust_pg via
    RustPgTransactionClient._run_under_savepoint_span() (hare/backends/rust_pg/client.py) - a separate
    reproduction of the same fix, since that wrapper calls self._active_tx.execute()/fetch_all()
    (the Rust PyO3 transaction object) directly, never through ConnectionWrapper/
    acquire_connection() at all."""

    class ForceRollback(Exception):
        pass

    savepoint_open = asyncio.Event()

    async def branch_with_savepoint(outer) -> None:
        try:
            async with outer._in_transaction() as inner:
                await Tournament.objects.using(inner).create(name="inside-savepoint")
                savepoint_open.set()
                await asyncio.sleep(0.05)
                raise ForceRollback("force rollback of this savepoint only")
        except ForceRollback:
            pass

    async def branch_ambient_write(outer) -> None:
        await savepoint_open.wait()
        await Tournament.objects.using(outer).create(name="ambient-write")

    async with Transactions.atomic("models") as outer:
        await asyncio.wait_for(
            asyncio.gather(branch_with_savepoint(outer), branch_ambient_write(outer)),
            timeout=5,
        )

    names = set(await Tournament.objects.all().values_list("name", flat=True))
    assert "ambient-write" in names, "never inside the rolled-back savepoint - must survive"
    assert "inside-savepoint" not in names


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_ambient_write_blocked_by_a_stuck_sibling_savepoint_raises_clear_error(db_truncate):
    """The fix for the race above bounds its own wait - a sibling that holds its savepoint open
    across an await unrelated to the database (waiting on another task/an external signal, not
    further DB work) must not be able to block an ambient write forever. Confirmed live before
    this timeout existed: this exact shape hung indefinitely instead of raising.

    branch_with_savepoint is deliberately never allowed to finish on its own (direct_write_done
    never gets set, since the ambient write times out instead of completing) - started as its
    own Task and explicitly cancelled in `finally` so it doesn't linger past this test.

    Runs on all 3 backends - see test_ambient_write_survives_a_gather_siblings_own_savepoint_
    rollback above for why rust_pg needs its own, separate fix/reproduction. The timeout constant
    is imported independently into each backend's own client module (a plain module-level name,
    not a shared mutable object), so patching it short for this test needs patching BOTH
    hare.dialects.base.client's copy (read by ConnectionWrapper, sqlite/asyncpg) AND, only when
    rust_pg is the backend under test, hare.dialects.postgresql.drivers.rust_pg.client's own copy (read by
    RustPgTransactionClient._run_under_savepoint_span) - patching only the former would leave rust_pg
    waiting out the real 30s default instead of the short timeout this test needs."""
    import hare.dialects.base.nested_savepoint_lock as client_module

    savepoint_open = asyncio.Event()
    direct_write_done = asyncio.Event()

    async def branch_with_savepoint(outer) -> None:
        async with outer._in_transaction() as inner:
            await Tournament.objects.using(inner).create(name="inside-savepoint")
            savepoint_open.set()
            await direct_write_done.wait()

    async def branch_ambient_write(outer) -> None:
        await savepoint_open.wait()
        await Tournament.objects.using(outer).create(name="ambient-write")
        direct_write_done.set()

    original_timeout = client_module.AMBIENT_QUERY_SAVEPOINT_WAIT_TIMEOUT_SECONDS
    client_module.AMBIENT_QUERY_SAVEPOINT_WAIT_TIMEOUT_SECONDS = 0.5
    try:
        async with Transactions.atomic("models") as outer:
            stuck_task = asyncio.ensure_future(branch_with_savepoint(outer))
            try:
                with pytest.raises(TransactionManagementError):
                    await branch_ambient_write(outer)
            finally:
                # Cancelled and awaited BEFORE this `async with` block exits - branch_with_
                # savepoint's own nested transaction must finish unwinding (its cancellation
                # propagates into `async with outer._in_transaction() as inner:`, rolling back
                # its savepoint) while the outer transaction is still open, not after.
                stuck_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await stuck_task
    finally:
        client_module.AMBIENT_QUERY_SAVEPOINT_WAIT_TIMEOUT_SECONDS = original_timeout


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_deeply_nested_mixed_gather_tree(db_truncate):
    """A genuinely mixed tree, not just one flat level of gather: outer -> middle ->
    gather(child_with_its_own_nested_grandchildren, plain_child). child_a opens its own further
    nested transaction off itself (sequential, single-task - the already-working case) AND its
    own gather() of two grandchildren off THAT (recursing the exact same hazard one level
    deeper), while child_b is a plain sibling doing simple work. Exercises the lock at three
    simultaneously-open levels, not just two."""

    async def grandchild(child_a, name: str) -> None:
        async with child_a._in_transaction() as inner:
            await Tournament.objects.using(inner).create(name=name)

    async def child_a(middle) -> None:
        async with middle._in_transaction() as child_a_wrapper:
            await Tournament.objects.using(child_a_wrapper).create(name="ChildA")
            await asyncio.wait_for(
                asyncio.gather(
                    grandchild(child_a_wrapper, "GrandchildA1"),
                    grandchild(child_a_wrapper, "GrandchildA2"),
                ),
                timeout=5,
            )

    async def child_b(middle) -> None:
        async with middle._in_transaction() as inner:
            await Tournament.objects.using(inner).create(name="ChildB")

    async with Transactions.atomic("models") as outer:
        async with outer._in_transaction() as middle:
            await asyncio.wait_for(asyncio.gather(child_a(middle), child_b(middle)), timeout=5)
            await Tournament.objects.using(middle).create(name="MiddleRow")

    names = set(await Tournament.objects.all().values_list("name", flat=True))
    assert names == {"ChildA", "GrandchildA1", "GrandchildA2", "ChildB", "MiddleRow"}


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_savepoint_lock_leaves_no_open_spans_after_gather_tree_completes(db_truncate):
    """Internal invariant, not just observable behavior: NestedSavepointLock's own bookkeeping
    (the open-spans stack every acquire()/release() pushes/pops) must end up completely empty
    once every nested block in a whole gather tree has exited - a leaked span here wouldn't
    necessarily fail THIS test's own data assertions, but would silently wedge every future
    nested transaction on this connection (a new acquire() waiting for a parent span that will
    never become current-top again)."""

    async def child(middle, name: str) -> None:
        async with middle._in_transaction() as inner:
            await Tournament.objects.using(inner).create(name=name)

    async with Transactions.atomic("models") as outer:
        savepoint_lock = outer._savepoint_lock
        async with outer._in_transaction() as middle:
            await asyncio.wait_for(asyncio.gather(child(middle, "A"), child(middle, "B")), timeout=5)
        assert savepoint_lock._open_spans == []

    assert savepoint_lock._open_spans == []


@pytest.mark.asyncio
async def test_get_own_ancestor_span_filters_out_a_span_owned_by_a_different_lock():
    """The unit under test for the cross-connection savepoint hang: a span read off
    current_savepoint_span while inside connection A's own nested savepoint must never be handed
    straight to connection B's own NestedSavepointLock.acquire() - B's _current_top() can never
    become a span it never created, so acquire() would wait for it forever (confirmed live: a
    misleading 30s "concurrent gather()/TaskGroup sibling" TransactionManagementError from a task
    that never existed). get_own_ancestor_span() must recognize a span (or a whole chain of
    spans) owned by a different lock as "no ancestor on THIS lock at all" (None), while still
    finding a lock's own span/descendant correctly."""
    lock_a = NestedSavepointLock()
    lock_b = NestedSavepointLock()

    span_a = await lock_a.acquire(None)
    nested_span_a = await lock_a.acquire(span_a)
    assert lock_b.get_own_ancestor_span(span_a) is None
    assert lock_b.get_own_ancestor_span(nested_span_a) is None

    span_b = await lock_b.acquire(None)
    nested_span_b = await lock_b.acquire(span_b)
    assert lock_b.get_own_ancestor_span(span_b) is span_b
    assert lock_b.get_own_ancestor_span(nested_span_b) is nested_span_b

    lock_b.release(nested_span_b)
    lock_b.release(span_b)
    lock_a.release(nested_span_a)
    lock_a.release(span_a)


@pytest.mark.asyncio
async def test_savepoint_span_opens_without_suspending_when_it_is_its_turn():
    """Every statement of a transaction takes a span - when no sibling holds one, acquire() must
    finish on its first step, without a pass of the event loop or an armed timeout."""
    lock = NestedSavepointLock()
    acquiring = lock.acquire(None, timeout_seconds=30)
    with pytest.raises(StopIteration) as stopped:
        acquiring.send(None)
    span = stopped.value.value
    assert span.parent is None
    lock.release(span)
    assert span.released


@pytest.mark.asyncio
async def test_savepoint_span_waits_for_the_sibling_then_opens():
    """A sibling's open span makes acquire() wait, release() lets it through, and a turn that
    never comes ends in TimeoutError."""
    lock = NestedSavepointLock()
    parent = await lock.acquire(None)
    sibling = await lock.acquire(parent)
    waiting = asyncio.ensure_future(lock.acquire(parent, timeout_seconds=5))
    await asyncio.sleep(0)
    assert not waiting.done()
    with pytest.raises(TimeoutError):
        await lock.acquire(parent, timeout_seconds=0.01)
    lock.release(sibling)
    second = await waiting
    assert second.parent is parent
    lock.release(second)
    lock.release(parent)
    assert not lock._waiters


@pytest.mark.asyncio
async def test_shielded_coroutine_starts_without_waiting_for_a_loop_pass():
    """A shielded COMMIT/ROLLBACK coroutine starts right away - one that finishes without
    suspending completes the whole call on its first step."""
    landed = Mock()

    async def finishes_at_once():
        return "done"

    shielded = TransactionClient._run_shielded_from_cancellation(finishes_at_once(), on_landed=landed)
    with pytest.raises(StopIteration) as stopped:
        shielded.send(None)
    assert stopped.value.value == "done"
    landed.assert_called_once_with()


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_sqlite_begin_is_one_hop_to_the_worker_thread(db_truncate):
    """The BEGIN, sent ahead of the transaction's first statement, ends a leftover transaction and
    begins in one call on aiosqlite's worker thread, not two."""
    real_connection = Connections.get("models")._connection
    original_execute = real_connection._execute
    worker_thread_calls = []

    async def counting_execute(function, *args, **kwargs):
        worker_thread_calls.append(function)
        return await original_execute(function, *args, **kwargs)

    async with Transactions.atomic():
        real_connection._execute = counting_execute
        try:
            await Tournament.objects.count()
        finally:
            real_connection._execute = original_execute
        assert [function.__name__ for function in worker_thread_calls[:-1]] == ["_commit_leftover_and_begin"]
        assert real_connection.in_transaction


@pytest_asyncio.fixture
async def two_independent_sqlite_aliases():
    """Two genuinely separate connections/NestedSavepointLocks: "alias_a" backs the ORM models,
    "alias_b" is a bare connection with a hand-rolled probe table (no model registration needed
    for it, avoiding a schema collision from registering tests.testmodels under two app labels).
    Unlike Transactions.autonomous(), which always shares its base connection's own database,
    this proves the fix isn't specific to autonomous() - two ordinary named aliases reach the
    exact same code path. Owns its cleanup (db_delete on both connections) rather than reusing
    test_two_databases.py's two_databases fixture, which never drops its own databases.

    Registering tests.testmodels here with "default_connection": "alias_a" stamps that value
    directly onto each model class's MetaInfo (a plain shared attribute, not scoped to any one
    HareContext - see Apps._build_initial_querysets()'s own "KNOWN LIMITATION" docstring).
    HareContext.__aexit__'s "reclaim previous context" step rebuilds the OUTER context's
    basequery/basetable bindings once this fixture's own context exits, but does not touch
    default_connection itself - previously invisible because every other test in this file used
    to get its own brand-new HareContext (db_isolated) that re-stamped every model fresh anyway.
    Now that most of this file shares one HareContext across the whole module (db_truncate), that
    same poisoned default_connection would otherwise persist into every later test. Restoring it
    explicitly here, back to whatever the outer (module-shared) context had it bound to, keeps
    this fixture's own effect fully self-contained regardless of which other fixtures the rest of
    the file uses."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    outer_ctx = HareContext.get_current()
    original_default_connections = (
        {model: model._meta.default_connection for model in outer_ctx.apps.apps.get("models", {}).values()}
        if outer_ctx is not None and outer_ctx.apps is not None
        else {}
    )
    ctx = HareContext()
    try:
        async with ctx:
            await ctx.init(
                config={
                    "connections": {
                        "alias_a": DbUrlConfigGenerator.expand(db_url, testing=True),
                        "alias_b": DbUrlConfigGenerator.expand(db_url, testing=True),
                    },
                    "apps": {
                        "models": {"models": ["tests.testmodels"], "default_connection": "alias_a"},
                    },
                },
                _create_db=True,
            )
            await ctx.generate_schemas()
            alias_b = ctx.connections.get("alias_b")
            # No PRIMARY KEY column - "INTEGER PRIMARY KEY" auto-increments on SQLite (a ROWID
            # alias) but NOT on Postgres, where it just rejects every INSERT that omits it.
            # Neither test using this table cares about an id at all, only that a write on
            # alias_b lands.
            await alias_b.execute_script("CREATE TABLE probe (name TEXT)")
            try:
                yield ctx
            finally:
                await ctx.connections.close_all(discard=False)
                for conn in ctx.connections.all():
                    await conn.db_delete()
                    ctx.connections.discard(conn.connection_name)
    finally:
        for model, default_connection in original_default_connections.items():
            model._meta.default_connection = default_connection


@requires_features(connection_name="alias_a", supports_transactions=True)
@pytest.mark.asyncio
async def test_two_named_aliases_second_opened_inside_first_nested_savepoint_does_not_hang(
    two_independent_sqlite_aliases,
):
    """Not specific to Transactions.autonomous() - two ordinary named aliases hit the exact same
    bug: an ordinary ambient query on alias_b, issued from code running inside an already-open
    nested savepoint on alias_a, reads alias_a's own span off current_savepoint_span and used to
    hand it straight to alias_b's own NestedSavepointLock - which could never make it alias_b's
    own topmost span, guaranteeing a 30s hang followed by a misleading "concurrent sibling"
    TransactionManagementError, even though everything here runs sequentially in one task.
    Wrapped in a tight wait_for so a regression fails in seconds, not 30+."""
    start = time.monotonic()

    async def scenario() -> None:
        async with Transactions.atomic("alias_a") as tx_a:
            async with tx_a._in_transaction() as inner_a:
                await Tournament.objects.using(inner_a).create(name="AliasA")
                async with Transactions.atomic("alias_b") as tx_b:
                    await tx_b.execute("INSERT INTO probe (name) VALUES ('AliasB')")

    await asyncio.wait_for(scenario(), timeout=10)

    elapsed = time.monotonic() - start
    assert elapsed < 5, f"should complete near-instantly, took {elapsed:.1f}s - the cross-connection span bug is back"

    alias_b = two_independent_sqlite_aliases.connections.get("alias_b")
    _, rows = await alias_b.execute("SELECT name FROM probe")
    assert [dict(row)["name"] for row in rows] == ["AliasB"]


@requires_features(connection_name="alias_a", supports_transactions=True)
@pytest.mark.asyncio
async def test_nested_savepoint_on_second_alias_opened_inside_first_alias_savepoint_does_not_hang(
    two_independent_sqlite_aliases,
):
    """A deeper variant of the same bug, found while fixing it: alias_b's OWN nested savepoint
    (not just an ordinary ambient query) opened from inside alias_a's nested savepoint hits
    NestedTransactionContext.__aenter__ instead of ConnectionWrapper._acquire_savepoint_span() -
    the identical current_savepoint_span.get() -> acquire() pattern, fixed the same way."""
    start = time.monotonic()

    async def scenario() -> None:
        async with Transactions.atomic("alias_a") as tx_a:
            async with tx_a._in_transaction() as inner_a:
                await Tournament.objects.using(inner_a).create(name="AliasA")
                async with Transactions.atomic("alias_b") as tx_b:
                    async with tx_b._in_transaction() as inner_b:
                        await inner_b.execute("INSERT INTO probe (name) VALUES ('AliasB')")

    await asyncio.wait_for(scenario(), timeout=10)

    elapsed = time.monotonic() - start
    assert elapsed < 5, f"should complete near-instantly, took {elapsed:.1f}s - the cross-connection span bug is back"

    alias_b = two_independent_sqlite_aliases.connections.get("alias_b")
    _, rows = await alias_b.execute("SELECT name FROM probe")
    assert [dict(row)["name"] for row in rows] == ["AliasB"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_transaction_decorator(db_truncate):
    """Test @atomic decorator with successful transaction."""

    @Transactions.atomic()
    async def bound_to_succeed():
        tournament = Tournament(name="Test")
        await tournament.save()
        await Tournament.objects.filter(id=tournament.id).update(name="Updated name")
        saved_event = await Tournament.objects.filter(name="Updated name").first()
        assert saved_event.id == tournament.id
        return tournament

    tournament = await bound_to_succeed()
    saved_event = await Tournament.objects.filter(name="Updated name").first()
    assert saved_event.id == tournament.id


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_transaction_decorator_defined_before_init(db_truncate):
    """Test @atomic decorator defined before Hare init."""
    tournament = await atomic_decorated_func()
    saved_event = await Tournament.objects.filter(name="Test").first()
    assert saved_event.id == tournament.id


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_transaction_decorator_fail(db_truncate):
    """Test @atomic decorator with failing transaction."""
    tournament = await Tournament.objects.create(name="Test")

    @Transactions.atomic()
    async def bound_to_fall():
        saved_event = await Tournament.objects.filter(name="Test").first()
        assert saved_event.id == tournament.id
        await Tournament.objects.filter(id=tournament.id).update(name="Updated name")
        saved_event = await Tournament.objects.filter(name="Updated name").first()
        assert saved_event.id == tournament.id
        raise OperationalError()

    with pytest.raises(OperationalError):
        await bound_to_fall()
    saved_event = await Tournament.objects.filter(name="Test").first()
    assert saved_event.id == tournament.id
    saved_event = await Tournament.objects.filter(name="Updated name").first()
    assert saved_event is None


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_transaction_with_m2m_relations(db_truncate):
    """Test transaction with M2M relations."""
    async with Transactions.atomic():
        tournament = await Tournament.objects.create(name="Test")
        event = await Event.objects.create(name="Test event", tournament=tournament)
        team = await Team.objects.create(name="Test team")
        await event.participants.add(team)
    assert [participant.id for participant in await event.participants.all()] == [team.id]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_transaction_exception_1(db_truncate):
    """Test double rollback raises TransactionManagementError."""
    with pytest.raises(TransactionManagementError):
        async with Transactions.atomic() as connection:
            await connection.rollback()
            await connection.rollback()


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_transaction_exception_2(db_truncate):
    """Test double commit raises TransactionManagementError."""
    with pytest.raises(TransactionManagementError):
        async with Transactions.atomic() as connection:
            await connection.commit()
            await connection.commit()


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_insert_await_across_transaction_fail(db_truncate):
    """Test insert await across transaction that fails."""
    tournament = Tournament(name="Test")
    query = tournament.save()  # pylint: disable=E1111

    try:
        async with Transactions.atomic():
            await query
            raise KeyError("moo")
    except KeyError:
        pass

    assert await Tournament.objects.all() == []


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_insert_rolled_back_restores_in_memory_instance_state(db_truncate):
    """Sibling of test_insert_await_across_transaction_fail, checking the IN-MEMORY instance
    itself, not just the DB's own content. save() used to unconditionally mark a freshly
    inserted instance _saved_in_db=True (and assign its DB-generated pk) with no rollback-restore
    registered for either - a transaction that rolled back for an unrelated reason left the
    instance falsely believing it was already a real, persisted row with a real pk, so a later,
    entirely legitimate save() on it raised IntegrityError ("Can't update object that doesn't
    exist") instead of correctly inserting it."""
    tournament = Tournament(name="Test")

    with pytest.raises(KeyError):
        async with Transactions.atomic():
            await tournament.save()
            assert tournament._saved_in_db is True
            assert tournament.pk is not None
            raise KeyError("moo")

    assert tournament._saved_in_db is False
    assert tournament.pk is None

    await tournament.save()
    assert tournament.pk is not None
    assert await Tournament.objects.all() == [tournament]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_insert_await_across_transaction_success(db_truncate):
    """Test insert await across transaction that succeeds."""
    tournament = Tournament(name="Test")
    query = tournament.save()  # pylint: disable=E1111

    async with Transactions.atomic():
        await query

    assert await Tournament.objects.all() == [tournament]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_update_await_across_transaction_fail(db_truncate):
    """Test update await across transaction that fails."""
    obj = await Tournament.objects.create(name="Test1")

    query = Tournament.objects.filter(id=obj.id).update(name="Test2")
    try:
        async with Transactions.atomic():
            await query
            raise KeyError("moo")
    except KeyError:
        pass

    assert await Tournament.objects.all().values("id", "name") == [{"id": obj.id, "name": "Test1"}]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_update_await_across_transaction_success(db_truncate):
    """Test update await across transaction that succeeds."""
    obj = await Tournament.objects.create(name="Test1")

    query = Tournament.objects.filter(id=obj.id).update(name="Test2")
    async with Transactions.atomic():
        await query

    assert await Tournament.objects.all().values("id", "name") == [{"id": obj.id, "name": "Test2"}]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_delete_await_across_transaction_fail(db_truncate):
    """Test delete await across transaction that fails."""
    obj = await Tournament.objects.create(name="Test1")

    query = Tournament.objects.filter(id=obj.id).delete()
    try:
        async with Transactions.atomic():
            await query
            raise KeyError("moo")
    except KeyError:
        pass

    assert await Tournament.objects.all().values("id", "name") == [{"id": obj.id, "name": "Test1"}]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_delete_await_across_transaction_success(db_truncate):
    """Test delete await across transaction that succeeds."""
    obj = await Tournament.objects.create(name="Test1")

    query = Tournament.objects.filter(id=obj.id).delete()
    async with Transactions.atomic():
        await query

    assert await Tournament.objects.all() == []


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_select_await_across_transaction_fail(db_truncate):
    """Test select await across transaction that fails."""
    try:
        async with Transactions.atomic():
            query = Tournament.objects.all().values("name")
            await Tournament.objects.create(name="Test1")
            result = await query
            raise KeyError("moo")
    except KeyError:
        pass

    assert result == [{"name": "Test1"}]
    assert await Tournament.objects.all() == []


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_select_await_across_transaction_success(db_truncate):
    """Test select await across transaction that succeeds."""
    async with Transactions.atomic():
        query = Tournament.objects.all().values("id", "name")
        obj = await Tournament.objects.create(name="Test1")
        result = await query

    assert result == [{"id": obj.id, "name": "Test1"}]
    assert await Tournament.objects.all().values("id", "name") == [{"id": obj.id, "name": "Test1"}]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_rollback_raising_exception(db_truncate):
    """Tests that if a rollback raises an exception, the connection context is restored."""
    conn = Connections.get("models")
    with pytest.raises(ValueError, match="rollback"):
        async with conn._in_transaction() as tx_conn:
            tx_conn.rollback = Mock(side_effect=ValueError("rollback"))
            raise ValueError("initial exception")

    assert Connections.get("models") == conn


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_commit_raising_exception(db_truncate):
    """Tests that if a commit raises an exception, the connection context is restored."""
    conn = Connections.get("models")
    with pytest.raises(ValueError, match="commit"):
        async with conn._in_transaction() as tx_conn:
            tx_conn.commit = Mock(side_effect=ValueError("commit"))

    assert Connections.get("models") == conn


def test_on_commit_outside_transaction_runs_immediately(db_truncate):
    """Test Transactions.on_commit() with no open transaction - callback runs right away, like Django."""
    fired = []
    Transactions.on_commit(lambda: fired.append("now"))
    assert fired == ["now"]


def test_on_commit_async_callback_outside_transaction_raises_without_calling_it(db_truncate):
    """Outside a transaction, on_commit() can't run an async callback synchronously - it must
    detect that up front (via inspect.iscoroutinefunction) and reject it via QueryError WITHOUT
    ever calling the async function. Calling it just to inspect whether the result is awaitable
    creates a real coroutine object that then never gets awaited or closed - Python emits
    "RuntimeWarning: coroutine was never awaited" once it's garbage-collected."""
    callback = AsyncMock()

    with pytest.raises(QueryError):
        Transactions.on_commit(callback)

    # AsyncMock.called/call_count are set synchronously the moment the mock is called (creating
    # its coroutine) - independent of whether that coroutine is ever awaited afterwards.
    assert callback.called is False


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_on_commit_fires_after_transaction_commits(db_truncate):
    """Test Transactions.on_commit() callback fires exactly once, only after the transaction commits."""
    fired = []
    async with Transactions.atomic():
        await Tournament.objects.create(name="Test")
        Transactions.on_commit(lambda: fired.append("committed"))
        assert fired == [], "must not fire before commit"
    assert fired == ["committed"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_on_commit_does_not_fire_on_rollback(db_truncate):
    """Test Transactions.on_commit() callback does not fire if the transaction rolls back."""
    fired = []
    with pytest.raises(SomeException):
        async with Transactions.atomic():
            Transactions.on_commit(lambda: fired.append("should-not-fire"))
            raise SomeException("boom")
    assert fired == []


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_on_commit_nested_savepoint_release_then_outer_commit(db_truncate):
    """Test a callback registered inside a nested savepoint that releases successfully fires once,
    when the OUTER transaction commits - not at the savepoint release itself."""
    fired = []
    async with Transactions.atomic():
        async with Transactions.atomic():
            Transactions.on_commit(lambda: fired.append("fired"))
        assert fired == [], "must not fire at savepoint release, only at outer commit"
    assert fired == ["fired"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_on_commit_nested_savepoint_release_then_outer_rollback(db_truncate):
    """Test a callback registered inside a nested savepoint that releases successfully does NOT
    fire if the OUTER transaction then rolls back."""
    fired = []
    with pytest.raises(SomeException):
        async with Transactions.atomic():
            async with Transactions.atomic():
                Transactions.on_commit(lambda: fired.append("should-not-fire"))
            raise SomeException("boom")
    assert fired == []


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_on_commit_discarded_on_own_savepoint_rollback(db_truncate):
    """Test a callback registered inside a nested savepoint that ITSELF rolls back is discarded,
    even though the outer transaction goes on to commit successfully."""
    fired = []
    async with Transactions.atomic():
        Transactions.on_commit(lambda: fired.append("outer"))
        with pytest.raises(SomeException):
            async with Transactions.atomic():
                Transactions.on_commit(lambda: fired.append("should-not-fire"))
                raise SomeException("boom")
    assert fired == ["outer"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_on_commit_fires_after_manual_top_level_commit(db_truncate):
    """A manual `await conn.commit()` (bypassing the async with block's own __aexit__ entirely,
    which never runs the commit/callback logic itself since client._finalized is already True
    by the time it checks) used to silently lose every on_commit() callback registered before
    it - confirmed live. commit() itself now runs them."""
    fired = []
    async with Transactions.atomic() as conn:
        Transactions.on_commit(lambda: fired.append("committed"))
        assert fired == [], "must not fire before commit"
        await conn.commit()
    assert fired == ["committed"]


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_sqlite_manual_commit_finalizes_before_on_commit_callback_raises(db_truncate):
    """SQLite's top-level commit() used to set _finalized=True only AFTER running its
    on_commit() callbacks, unlike asyncpg/rust_pg - confirmed live. That left _finalized False
    if a callback raised, even though the real SQL COMMIT had already landed, so __aexit__'s
    _commit_or_rollback (whose own docstring asserts client._finalized is already True by the
    time it runs, for every backend) saw `not client._finalized` and issued a spurious extra
    rollback() call once the callback's exception propagated out of the `async with` block.
    commit() now flips _finalized right after the real COMMIT succeeds, before running
    callbacks, matching every other backend."""
    tournament = Tournament(name="Test")
    finalized_during_callback = []

    async def raising_callback():
        finalized_during_callback.append(conn._finalized)
        raise SomeException("boom")

    with pytest.raises(SomeException):
        async with Transactions.atomic() as conn:
            conn.rollback = AsyncMock()
            await tournament.save()
            Transactions.on_commit(raising_callback)
            await conn.commit()

    assert finalized_during_callback == [True], "commit() must finalize before running callbacks"
    assert conn._finalized is True
    conn.rollback.assert_not_awaited()

    # The real COMMIT already took effect - the callback's exception propagating out of the
    # `async with` block must not trigger a spurious rollback that undoes it.
    saved = await Tournament.objects.filter(id=tournament.id).first()
    assert saved is not None


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_sqlite_cancelled_commit_still_lands_and_finalizes(db_truncate):
    """aiosqlite dispatches the real sqlite3 COMMIT to a single background worker thread via a
    queue - once the worker pulls it off the queue it runs to completion no matter what happens
    on the asyncio side afterwards, so cancelling the task awaiting commit() only detaches the
    awaiting Future early and aiosqlite silently drops the eventual result (its
    set_result()/set_exception() both no-op on an already-cancelled Future). commit() used to
    set _finalized=True only after its own await returned, so a task cancelled while the real
    COMMIT was still running in the worker thread left _finalized False even though the write
    had already landed permanently - confirmed live via this same monkeypatch technique.
    commit() now shields the real DB call so the flag and the real COMMIT always land together,
    and CancelledError only propagates after that."""
    base_client = Connections.get("models")
    real_connection = base_client._connection
    original_commit = real_connection.commit

    loop = asyncio.get_event_loop()
    commit_started = asyncio.Event()

    def patched_commit(self):
        # Only the end-of-transaction COMMIT goes through aiosqlite's commit() - begin() ends a
        # leftover transaction on the worker thread directly, together with its BEGIN.

        async def _commit():
            def slow_commit():
                # Runs on aiosqlite's background worker thread - by the time this fires, the
                # real commit is irrevocably queued and about to run, regardless of whether the
                # asyncio side is later cancelled.
                loop.call_soon_threadsafe(commit_started.set)
                time.sleep(0.3)
                return self._conn.commit()

            future = loop.create_future()
            self._tx.put_nowait((future, slow_commit))
            return await future

        return _commit()

    real_connection.commit = types.MethodType(patched_commit, real_connection)

    tournament = Tournament(name="cancelled-commit")
    captured: dict = {}

    async def do_transaction():
        # __aenter__ and __aexit__ must run in the same task - Connections.current()'s set()/
        # reset() pair is ContextVar-based and a Task copies the context at creation, so
        # entering in one task and exiting in another (e.g. the outer test coroutine vs. a task
        # spawned to make the exit cancellable) raises "Token was created in a different
        # Context". Cancelling this whole task from outside still lands the cancellation mid
        # commit(), which is all this test needs.
        async with Transactions.atomic() as conn:
            captured["wrapper"] = conn
            conn.rollback = AsyncMock()
            await tournament.save()

    try:
        task = asyncio.ensure_future(do_transaction())
        await asyncio.wait_for(commit_started.wait(), timeout=2)
        # The worker thread is now genuinely mid-commit (inside its 0.3s sleep) - cancel here to
        # reproduce a cancellation landing while the real COMMIT is irrevocably in flight.
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        real_connection.commit = original_commit

    conn = captured["wrapper"]
    assert conn._finalized is True, "commit() must finalize once the real COMMIT actually lands"
    conn.rollback.assert_not_awaited()

    # The real COMMIT already landed - a caller retrying after CancelledError must see it, not a
    # dropped write.
    saved = await Tournament.objects.filter(id=tournament.id).first()
    assert saved is not None

    # The connection isn't left corrupted - a subsequent transaction on it still works normally.
    other = Tournament(name="after-cancelled-commit")
    async with Transactions.atomic():
        await other.save()
    assert await Tournament.objects.filter(id=other.id).first() is not None


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_sqlite_cancelled_commit_still_runs_on_commit_callbacks(db_truncate):
    """commit() used to shield only the real COMMIT itself, then run on_commit() callbacks via a
    SEPARATE _run_shielded_from_cancellation() call afterward - a cancellation landing in the gap
    between those two calls let the real COMMIT land (and _finalized get set) while the second
    call's shield never even started, silently dropping every on_commit() callback with no trace.
    Both now run inside ONE shielded coroutine - see commit()'s own comment."""
    base_client = Connections.get("models")
    real_connection = base_client._connection
    original_commit = real_connection.commit

    loop = asyncio.get_event_loop()
    commit_started = asyncio.Event()

    def patched_commit(self):
        async def _commit():
            def slow_commit():
                loop.call_soon_threadsafe(commit_started.set)
                time.sleep(0.3)
                return self._conn.commit()

            future = loop.create_future()
            self._tx.put_nowait((future, slow_commit))
            return await future

        return _commit()

    real_connection.commit = types.MethodType(patched_commit, real_connection)

    tournament = Tournament(name="cancelled-commit-callback")
    on_commit_callback = Mock()
    captured: dict = {}

    async def do_transaction():
        async with Transactions.atomic() as conn:
            captured["wrapper"] = conn
            conn.rollback = AsyncMock()
            await tournament.save()
            Transactions.on_commit(on_commit_callback)

    try:
        task = asyncio.ensure_future(do_transaction())
        await asyncio.wait_for(commit_started.wait(), timeout=2)
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        real_connection.commit = original_commit

    conn = captured["wrapper"]
    assert conn._finalized is True
    on_commit_callback.assert_called_once()


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_sqlite_savepoint_translates_driver_exceptions(db_truncate):
    """savepoint() wasn't wrapped in @translate_exceptions, unlike every other transactional
    method on SqliteTransactionClient (commit/rollback/savepoint_rollback/release_savepoint) -
    a raw sqlite3.OperationalError from the SAVEPOINT statement leaked through instead of
    hare.exceptions.OperationalError, a real behavioral gap for any caller catching the hare
    exception type around Transactions.atomic() nesting."""
    async with Transactions.atomic() as outer:
        real_execute = outer._connection.execute

        async def broken_execute(sql, *args, **kwargs):
            if sql.startswith("SAVEPOINT"):
                raise sqlite3.OperationalError("simulated savepoint failure")
            return await real_execute(sql, *args, **kwargs)

        outer._connection.execute = broken_execute
        try:
            with pytest.raises(OperationalError):
                async with outer._in_transaction():
                    # The SAVEPOINT goes out ahead of the first statement under it.
                    await Tournament.objects.count()
        finally:
            outer._connection.execute = real_execute


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_sqlite_cancelled_savepoint_rollback_still_lands_and_updates_state(db_truncate):
    """savepoint_rollback()'s ROLLBACK TO goes through the exact same queued-to-a-background-
    worker-thread dispatch as commit() - and NestedTransactionContext.__aexit__ (base/client.py)
    calls it directly on the automatic `async with inner:` exit path, bypassing the shield that
    commit()/rollback() apply to their own call sites entirely. Before shielding
    savepoint_rollback() itself, a cancellation landing mid ROLLBACK TO left self._savepoint
    still pointing at a savepoint whose rollback had already run, and skipped discarding the
    on_commit() callbacks registered inside it - the identical bookkeeping-vs-reality gap as the
    reported commit() bug, just one level down. savepoint_rollback() now shields the real DB
    call the same way, so state and the real ROLLBACK TO always land together."""
    real_connection = Connections.get("models")._connection
    original_execute = real_connection._execute

    loop = asyncio.get_event_loop()
    rollback_to_started = asyncio.Event()

    async def patched_execute(self, fn, *args, **kwargs):
        if not (args and isinstance(args[0], str) and args[0].startswith("ROLLBACK TO ")):
            return await original_execute(fn, *args, **kwargs)

        def slow_call():
            # Runs on aiosqlite's background worker thread - by the time this fires, the real
            # ROLLBACK TO is irrevocably queued and about to run, regardless of whether the
            # asyncio side is later cancelled.
            loop.call_soon_threadsafe(rollback_to_started.set)
            time.sleep(0.3)
            return fn(*args, **kwargs)

        future = loop.create_future()
        self._tx.put_nowait((future, slow_call))
        return await future

    real_connection._execute = types.MethodType(patched_execute, real_connection)

    fired = []
    captured: dict = {}

    async def do_transaction():
        # Both the outer and the nested __aenter__/__aexit__ must run in the same task, for the
        # same ContextVar-token reason as test_sqlite_cancelled_commit_still_lands_and_finalizes
        # above. Cancelling this whole task from outside still lands the cancellation mid
        # savepoint_rollback(), which is all this test needs.
        async with Transactions.atomic():
            with pytest.raises(SomeException):
                async with Transactions.atomic() as inner:
                    captured["inner"] = inner
                    Transactions.on_commit(lambda: fired.append("should-be-discarded"))
                    await Tournament.objects.create(name="inner-savepoint-rollback")
                    raise SomeException("boom")

    try:
        task = asyncio.ensure_future(do_transaction())
        await asyncio.wait_for(rollback_to_started.wait(), timeout=2)
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        real_connection._execute = original_execute

    inner = captured["inner"]
    assert inner._finalized is True, "savepoint_rollback() must finalize the savepoint once it lands"
    assert fired == [], "the discarded on_commit() callback must never fire"

    # The real ROLLBACK TO already ran - the row created inside the savepoint must be gone.
    assert await Tournament.objects.filter(name="inner-savepoint-rollback").first() is None

    # The connection isn't left corrupted - a subsequent transaction on it still works normally.
    other = Tournament(name="after-cancelled-savepoint-rollback")
    async with Transactions.atomic():
        await other.save()
    assert await Tournament.objects.filter(id=other.id).first() is not None


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_sqlite_abandoned_savepoint_rollback_blocks_a_sibling_nested_transaction(db_truncate, monkeypatch):
    """NestedTransactionContext.__aexit__ (base/client.py) used to release its own
    NestedSavepointLock span unconditionally in its `finally` block, even when the
    savepoint_rollback()/release_savepoint() it just called hit SHIELDED_CANCELLATION_WAIT_
    TIMEOUT_SECONDS and left its real ROLLBACK TO/RELEASE running, abandoned, in the background
    (see _run_shielded_from_cancellation's own docstring). A sibling nested transaction spawned
    right after that - blocked on the SAME span via NestedSavepointLock.acquire() - used to be let
    through immediately instead of waiting for the abandoned operation to actually finish,
    letting it issue its own SAVEPOINT/RELEASE/ROLLBACK TO on the same physical connection while
    the abandoned one might still be mid-flight against it - exactly the stack corruption
    NestedSavepointLock exists to prevent, just reached through a different path than a plain
    interleaving race.

    Forces genuine abandonment (not just "landed a bit late") by monkeypatching the wait bound
    down far below the simulated ROLLBACK TO's own duration, mirroring
    test_sqlite_cancelled_savepoint_rollback_still_lands_and_updates_state's own technique for
    simulating a slow aiosqlite background-thread call."""
    from hare.dialects.base.client import transaction_client as client_module

    monkeypatch.setattr(client_module, "SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS", 0.05)

    real_connection = Connections.get("models")._connection
    original_execute = real_connection._execute

    loop = asyncio.get_event_loop()
    rollback_to_started = asyncio.Event()
    rollback_to_finished = asyncio.Event()
    # Captured the moment the sibling's OWN "SAVEPOINT ..." call reaches this patch - BEFORE it's
    # even queued onto aiosqlite's single background worker thread, which (independently of
    # NestedSavepointLock) already serializes execution of the two calls relative to each other
    # regardless of whether the Python-level span was released early or not. That serialization
    # would otherwise mask a released-too-early span in a pure wall-clock timing check - this
    # records what the Python-level code had ALREADY DECIDED to do (attempt the SAVEPOINT at
    # all) at the moment it reached this point, before either call has necessarily finished
    # executing.
    savepoint_reached_before_rollback_to_landed: bool | None = None

    async def patched_execute(self, fn, *args, **kwargs):
        nonlocal savepoint_reached_before_rollback_to_landed
        if (
            rollback_to_started.is_set()
            and savepoint_reached_before_rollback_to_landed is None
            and args
            and isinstance(args[0], str)
            and args[0].startswith("SAVEPOINT ")
        ):
            # Only the SIBLING's own SAVEPOINT can reach here (rollback_to_started already set) -
            # the INNER transaction's own SAVEPOINT, at the start of this test, ran well before
            # that.
            savepoint_reached_before_rollback_to_landed = not rollback_to_finished.is_set()
        if not (args and isinstance(args[0], str) and args[0].startswith("ROLLBACK TO ")):
            return await original_execute(fn, *args, **kwargs)

        def slow_call():
            # Runs on aiosqlite's background worker thread - well past
            # SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS (0.05s above), so
            # _run_shielded_from_cancellation() genuinely gives up and abandons this in the
            # background instead of waiting for it.
            loop.call_soon_threadsafe(rollback_to_started.set)
            time.sleep(0.3)
            result = fn(*args, **kwargs)
            loop.call_soon_threadsafe(rollback_to_finished.set)
            return result

        future = loop.create_future()
        self._tx.put_nowait((future, slow_call))
        return await future

    real_connection._execute = types.MethodType(patched_execute, real_connection)

    try:
        async with Transactions.atomic() as outer:

            async def cancelled_inner():
                with pytest.raises(SomeException):
                    async with outer._in_transaction():
                        await Tournament.objects.create(name="abandoned-savepoint-rollback")
                        raise SomeException("boom")

            inner_task = asyncio.ensure_future(cancelled_inner())
            await asyncio.wait_for(rollback_to_started.wait(), timeout=2)
            await asyncio.sleep(0.02)
            inner_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await inner_task

            # The abandoned ROLLBACK TO is still sleeping in the background at this point (it
            # takes 0.3s; SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS only waited 0.05s before
            # giving up) - a sibling nested transaction spawned now must not be able to proceed
            # until that real operation has actually finished.
            assert not rollback_to_finished.is_set()

            async with outer._in_transaction():
                # The SAVEPOINT goes out ahead of the first statement under it.
                await Tournament.objects.count()

            assert rollback_to_finished.is_set(), (
                "the sibling must not have been able to acquire the span before the abandoned "
                "ROLLBACK TO actually finished"
            )
            assert savepoint_reached_before_rollback_to_landed is False, (
                "the sibling's own SAVEPOINT reached the connection before the abandoned ROLLBACK "
                "TO had actually finished - the span was released too early, letting the sibling "
                "proceed while the abandoned operation might still be mid-flight on the same "
                "connection (aiosqlite's own single background thread happens to still serialize "
                "the two calls' EXECUTION either way, which is why this checks when the SAVEPOINT "
                "was attempted, not how long it took to run)"
            )
    finally:
        real_connection._execute = original_execute

    # The connection isn't left corrupted - a subsequent transaction on it still works normally.
    other = Tournament(name="after-abandoned-savepoint-rollback")
    async with Transactions.atomic():
        await other.save()
    assert await Tournament.objects.filter(id=other.id).first() is not None


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_on_commit_discarded_on_manual_savepoint_rollback(db_truncate):
    """A manual `await inner.rollback()` on a nested savepoint (bypassing
    NestedTransactionContext's own __aexit__, the only place that used to discard callbacks
    registered inside it) used to leave those callbacks queued to run at the outer commit
    anyway, even though the code that registered them was rolled back - confirmed live.
    rollback()/savepoint_rollback() themselves now discard them."""
    fired = []
    async with Transactions.atomic():
        Transactions.on_commit(lambda: fired.append("outer"))
        async with Transactions.atomic() as inner:
            Transactions.on_commit(lambda: fired.append("should-not-fire"))
            await inner.rollback()
    assert fired == ["outer"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_on_commit_async_callback(db_truncate):
    """Test Transactions.on_commit() accepts a coroutine function, not just a plain callable."""
    fired = []

    async def callback():
        fired.append("async")

    async with Transactions.atomic():
        Transactions.on_commit(callback)
    assert fired == ["async"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_on_commit_one_callback_failing_does_not_skip_others(db_truncate):
    """Test that if one Transactions.on_commit() callback raises, sibling callbacks still run, and the error
    surfaces to the caller afterwards."""
    fired = []

    def failing():
        raise ValueError("callback failure")

    with pytest.raises(ValueError, match="callback failure"):
        async with Transactions.atomic():
            Transactions.on_commit(failing)
            Transactions.on_commit(lambda: fired.append("ran anyway"))
    assert fired == ["ran anyway"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_on_commit_two_callbacks_failing_raises_exception_group(db_truncate):
    """Test that if two Transactions.on_commit() callbacks both raise, both exceptions surface
    together via an ExceptionGroup instead of the second one being silently dropped - confirmed
    live to previously vanish without a trace (not even in __context__/__cause__)."""
    fired = []

    def first_failing():
        raise SomeException("first failure")

    def second_failing():
        raise ValueError("second failure")

    with pytest.raises(ExceptionGroup) as exc_info:
        async with Transactions.atomic():
            Transactions.on_commit(first_failing)
            Transactions.on_commit(second_failing)
            Transactions.on_commit(lambda: fired.append("ran anyway"))
    assert fired == ["ran anyway"]
    exceptions = exc_info.value.exceptions
    assert len(exceptions) == 2
    assert isinstance(exceptions[0], SomeException)
    assert isinstance(exceptions[1], ValueError)


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_on_commit_cancellation_mid_callbacks_still_runs_remaining_callbacks(db_truncate):
    """A cancellation landing while on_commit() callbacks are still being iterated over must not
    lose any callback still pending - confirmed live: before shielding the callback loop the same
    way the real COMMIT is already shielded, a callback after the one running when cancellation
    landed never ran, with no trace of it being skipped. Cancellation must instead be deferred
    until every callback has actually run, matching the COMMIT's own semantics."""
    fired = []
    slow_started = asyncio.Event()

    async def slow_callback():
        slow_started.set()
        await asyncio.sleep(0.2)
        fired.append("slow")

    async def fast_callback():
        fired.append("fast")

    async def do_transaction():
        async with Transactions.atomic():
            Transactions.on_commit(slow_callback)
            Transactions.on_commit(fast_callback)
            await Tournament(name="cancel-mid-callbacks").save()

    task = asyncio.ensure_future(do_transaction())
    await asyncio.wait_for(slow_started.wait(), timeout=2)
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert fired == ["slow", "fast"], "cancellation must defer until all callbacks finish, not lose any"


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_on_rollback_fires_after_transaction_rollback(db_truncate):
    """Test Transactions.on_rollback() callback fires exactly once, only after the transaction
    actually rolls back."""
    fired = []
    with pytest.raises(SomeException):
        async with Transactions.atomic():
            Transactions.on_rollback(lambda: fired.append("rolled-back"))
            assert fired == [], "must not fire before rollback"
            raise SomeException("boom")
    assert fired == ["rolled-back"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_on_rollback_discarded_on_transaction_commit(db_truncate):
    """Test Transactions.on_rollback() callback is discarded, never fired, if the transaction
    commits normally - nothing was ever undone for it to compensate."""
    fired = []
    async with Transactions.atomic():
        await Tournament.objects.create(name="Test")
        Transactions.on_rollback(lambda: fired.append("should-not-fire"))
    assert fired == []


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_on_rollback_fires_immediately_on_own_savepoint_rollback(db_truncate):
    """A callback registered inside a nested savepoint that ITSELF rolls back fires right there -
    that scope's own effects genuinely never took hold, unlike a savepoint that merely releases
    (which could still be undone later by an outer rollback) - regardless of what the outer
    transaction goes on to do afterwards."""
    fired = []
    async with Transactions.atomic():
        with pytest.raises(SomeException):
            async with Transactions.atomic():
                Transactions.on_rollback(lambda: fired.append("fired"))
                raise SomeException("boom")
        assert fired == ["fired"], "must fire immediately at the savepoint's own rollback"
    assert fired == ["fired"], "must not fire a second time when the outer transaction commits"


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_on_rollback_inherited_by_outer_transaction_after_savepoint_release(db_truncate):
    """A callback registered inside a nested savepoint that releases successfully is NOT
    discarded - it's inherited by the outer transaction, and fires if THAT later rolls back."""
    fired = []
    with pytest.raises(SomeException):
        async with Transactions.atomic():
            async with Transactions.atomic():
                Transactions.on_rollback(lambda: fired.append("inherited"))
            assert fired == [], "must not fire at the savepoint's own successful release"
            raise SomeException("boom")
    assert fired == ["inherited"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_on_rollback_inherited_by_outer_transaction_discarded_on_outer_commit(db_truncate):
    """The same inherited-callback case as above, but the outer transaction commits instead -
    the callback must stay discarded, never fired."""
    fired = []
    async with Transactions.atomic():
        async with Transactions.atomic():
            Transactions.on_rollback(lambda: fired.append("should-not-fire"))
    assert fired == []


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_on_rollback_outside_transaction_raises_params_error(db_truncate):
    """Unlike on_commit(), on_rollback() has no "run immediately" fallback outside a transaction -
    a rollback that can never happen has nothing sensible to attach a callback to."""
    with pytest.raises(QueryError):
        Transactions.on_rollback(lambda: None)


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_failed_begin_does_not_corrupt_connection_alias(db_truncate):
    """A failed begin() must not leave the connection alias permanently pointing at the
    half-initialized transactional wrapper, nor leak the lock/pooled connection it acquired -
    __aexit__ never runs when __aenter__ itself raises (async-context-manager protocol), so
    both TransactionContextPooled and SqliteTransactionContext must clean up before
    re-raising, not just on the normal exit path."""
    base_client = Connections.get("models")
    probe_ctx = base_client._in_transaction()
    wrapper = getattr(probe_ctx, "connection", None) or getattr(probe_ctx, "client", None)
    wrapper_cls = type(wrapper)

    async def failing_begin(self):
        raise SomeException("simulated begin() failure")

    original_begin = wrapper_cls.begin
    wrapper_cls.begin = failing_begin
    try:
        with pytest.raises(SomeException):
            async with Transactions.atomic():
                pass
    finally:
        wrapper_cls.begin = original_begin

    # The alias must still resolve to the original base client, not the leaked wrapper.
    assert Connections.get("models") is base_client


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_failed_ensure_connection_releases_the_acquire_connection_lock(db_truncate):
    """The non-transactional connection-acquire wrapper (ConnectionWrapper on SQLite - a real
    held-for-the-whole-acquire lock; PoolConnectionWrapper on pooled dialects, which only
    scopes its lock around pool initialization) must not wedge or leak state if
    ensure_connection() raises - __aexit__ never runs when __aenter__ itself raises
    (async-context-manager protocol)."""
    client = Connections.get("models")
    wrapper = client.acquire_connection()

    async def failing_ensure_connection(self):
        raise SomeException("simulated create_connection failure")

    original_ensure_connection = type(wrapper).ensure_connection
    type(wrapper).ensure_connection = failing_ensure_connection
    try:
        with pytest.raises(SomeException):
            async with wrapper:
                pass
    finally:
        type(wrapper).ensure_connection = original_ensure_connection

    # ConnectionWrapper (SQLite) holds one lock across the whole acquire - it must be released.
    if hasattr(wrapper, "_lock"):
        assert not wrapper._lock.locked()

    # A subsequent acquire must not be wedged/broken by the earlier failure, on either wrapper.
    async with client.acquire_connection():
        pass

    # A subsequent transaction must work normally - the lock/pooled connection wasn't leaked.
    async with Transactions.atomic():
        await Tournament.objects.create(name="after failed begin")
    assert await Tournament.objects.filter(name="after failed begin").exists()


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_close_inside_transaction_raises_instead_of_tearing_down_the_connection(db_truncate):
    """close() on the transactional wrapper used to fall through to the base client's own
    close() - on rust_pg this actually tore down the SHARED connection pool (the wrapper's
    self._pool aliases the same pool object the outer, non-transactional client still relies on
    for every other query on this alias), and on every backend it then crashed on
    self._template/self.filename (never set on a transactional wrapper) before the caller could
    even see what went wrong. Closing a connection while a transaction is still open on it isn't
    a coherent operation on any backend."""
    async with Transactions.atomic() as conn:
        with pytest.raises(TransactionManagementError):
            await conn.close()

    # The shared connection/pool must still be fully usable afterwards, on this same alias.
    await Tournament.objects.create(name="after close-inside-transaction attempt")
    assert await Tournament.objects.filter(name="after close-inside-transaction attempt").exists()

    async with Transactions.atomic():
        await Tournament.objects.create(name="second transaction after close-inside-transaction attempt")
    assert await Tournament.objects.filter(name="second transaction after close-inside-transaction attempt").exists()


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_finalized_wrapper_rejects_query_after_commit(db_truncate):
    """Only commit()/rollback() themselves used to check `_finalized` (purely to guard against a
    double call) - no query-executing method anywhere checked it, so a stale reference to an
    already-committed transaction wrapper (e.g. an orphaned task left over after
    asyncio.gather() where a sibling raised) could still issue queries through it. Confirmed
    live on sqlite: a row inserted via such a stale wrapper after commit() was actually
    persisted. execute() (funnelled through every backend's shared translate_exceptions
    decorator) must now reject it instead."""
    async with Transactions.atomic() as conn:
        await Tournament.objects.create(name="inside-committed-tx")
        await conn.commit()

    assert conn._finalized is True
    with pytest.raises(TransactionManagementError):
        await conn.execute("SELECT 1")


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_finalized_wrapper_rejects_query_after_rollback(db_truncate):
    """Same gap as test_finalized_wrapper_rejects_query_after_commit above, for rollback()."""
    captured = {}
    with pytest.raises(SomeException):
        async with Transactions.atomic() as conn:
            captured["conn"] = conn
            await Tournament.objects.create(name="inside-rolled-back-tx")
            raise SomeException("boom")

    conn = captured["conn"]
    assert conn._finalized is True
    with pytest.raises(TransactionManagementError):
        await conn.execute("SELECT 1")


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_stale_wrapper_write_after_commit_no_longer_persists(db_truncate):
    """The live-confirmed consequence of the gap above: an actual write through a stale,
    already-committed wrapper must be rejected before it reaches the database, not silently
    executed and persisted."""
    async with Transactions.atomic() as conn:
        await Tournament.objects.create(name="inside-committed-tx")
        await conn.commit()

    with pytest.raises(TransactionManagementError):
        await conn.execute("INSERT INTO tournament (name, created) VALUES (?, datetime('now'))", ["stale-write"])
    assert await Tournament.objects.filter(name="stale-write").first() is None


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_savepoint_wrapper_finalizes_and_rejects_queries_after_release(db_truncate):
    """release_savepoint() on sqlite used to never set `_finalized` at all (unlike commit()/
    rollback(), and unlike asyncpg/rust_pg's own release_savepoint(), which already delegate
    into commit() and set it) - a documented cross-backend inconsistency. Once a savepoint is
    released, a stale reference to that nested wrapper must be finalized the same way a
    top-level commit finalizes its own wrapper."""
    async with Transactions.atomic():
        async with Transactions.atomic() as inner:
            await Tournament.objects.create(name="inside-released-savepoint")
        assert inner._finalized is True
        with pytest.raises(TransactionManagementError):
            await inner.execute("SELECT 1")

    assert await Tournament.objects.filter(name="inside-released-savepoint").exists()


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_savepoint_wrapper_finalizes_and_rejects_queries_after_rollback(db_truncate):
    """Same gap as test_savepoint_wrapper_finalizes_and_rejects_queries_after_release above, for
    savepoint_rollback() - sqlite's own savepoint_rollback() used to leave `_finalized` False."""
    captured = {}
    async with Transactions.atomic():
        with pytest.raises(SomeException):
            async with Transactions.atomic() as inner:
                captured["inner"] = inner
                await Tournament.objects.create(name="inside-rolled-back-savepoint")
                raise SomeException("boom")

    inner = captured["inner"]
    assert inner._finalized is True
    with pytest.raises(TransactionManagementError):
        await inner.execute("SELECT 1")
    assert await Tournament.objects.filter(name="inside-rolled-back-savepoint").first() is None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_asyncpg_cancelled_commit_still_lands_and_finalizes(db_truncate):
    """AsyncpgClient's commit()/rollback() used to run the real `Transaction.commit()`/
    `Transaction.rollback()` call unshielded, unlike sqlite/rust_pg's equivalents - a task
    cancelled while that await was in flight could see a bare CancelledError with `_finalized`
    left False, even though the real COMMIT had, in fact, already landed.

    A plain `task.cancel()` while the real network round trip to Postgres is still in flight
    does not reproduce this directly: asyncpg's own protocol reacts to a cancelled query by
    sending a genuine Postgres CancelRequest, which (confirmed live via a pg_sleep() inside a
    DEFERRED constraint trigger) actually aborts the in-flight COMMIT server-side rather than
    leaving it to land silently. The real race is narrower - cancellation landing in the gap
    AFTER Postgres has already acknowledged the COMMIT but BEFORE `self._finalized = True` runs
    - so it's forced directly here: letting the real `Transaction.commit()` complete first
    (proving the COMMIT already 100% landed), then introducing an artificial cancellable await
    only reachable once the real work is done.

    commit() now shields the real call via asyncio.shield(), so the flag and the real COMMIT
    always land together, and the shielded Task is never itself the target of asyncpg's own
    CancelRequest machinery."""
    if "hare.dialects.postgresql.drivers.asyncpg" not in type(db_truncate.db()).__module__:
        pytest.skip(
            "asyncpg-specific: monkey-patches asyncpg.transaction.Transaction directly, "
            "which rust_pg never calls (it doesn't use the asyncpg library at all)"
        )

    import asyncpg.transaction

    original_commit = asyncpg.transaction.Transaction._Transaction__commit
    real_commit_landed = asyncio.Event()

    async def patched_commit(self):
        await original_commit(self)
        real_commit_landed.set()
        await asyncio.sleep(0.3)

    asyncpg.transaction.Transaction._Transaction__commit = patched_commit

    tournament = Tournament(name="cancelled-commit-asyncpg")
    captured: dict = {}

    async def do_transaction():
        async with Transactions.atomic() as conn:
            captured["wrapper"] = conn
            await tournament.save()

    try:
        task = asyncio.ensure_future(do_transaction())
        await asyncio.wait_for(real_commit_landed.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        asyncpg.transaction.Transaction._Transaction__commit = original_commit

    conn = captured["wrapper"]
    assert conn._finalized is True, "commit() must finalize once the real COMMIT actually lands"

    saved = await Tournament.objects.filter(id=tournament.id).first()
    assert saved is not None

    # The connection isn't left corrupted - a subsequent transaction on it still works normally.
    other = Tournament(name="after-cancelled-commit-asyncpg")
    async with Transactions.atomic():
        await other.save()
    assert await Tournament.objects.filter(id=other.id).first() is not None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_asyncpg_cancelled_commit_still_runs_on_commit_callbacks(db_truncate):
    """commit() used to shield only the real COMMIT itself, then run on_commit() callbacks via a
    SEPARATE _run_shielded_from_cancellation() call afterward - a cancellation landing in the gap
    between those two calls let the real COMMIT land (and _finalized get set) while the second
    call's shield never even started, silently dropping every on_commit() callback with no trace.
    Both now run inside ONE shielded coroutine - see commit()'s own comment. Uses the same
    monkeypatch technique as test_asyncpg_cancelled_commit_still_lands_and_finalizes - see its own
    docstring for why a plain task.cancel() doesn't reproduce this directly."""
    if "hare.dialects.postgresql.drivers.asyncpg" not in type(db_truncate.db()).__module__:
        pytest.skip(
            "asyncpg-specific: monkey-patches asyncpg.transaction.Transaction directly, "
            "which rust_pg never calls (it doesn't use the asyncpg library at all)"
        )

    import asyncpg.transaction

    original_commit = asyncpg.transaction.Transaction._Transaction__commit
    real_commit_landed = asyncio.Event()

    async def patched_commit(self):
        await original_commit(self)
        real_commit_landed.set()
        await asyncio.sleep(0.3)

    asyncpg.transaction.Transaction._Transaction__commit = patched_commit

    tournament = Tournament(name="cancelled-commit-callback-asyncpg")
    on_commit_callback = Mock()
    captured: dict = {}

    async def do_transaction():
        async with Transactions.atomic() as conn:
            captured["wrapper"] = conn
            await tournament.save()
            Transactions.on_commit(on_commit_callback)

    try:
        task = asyncio.ensure_future(do_transaction())
        await asyncio.wait_for(real_commit_landed.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        asyncpg.transaction.Transaction._Transaction__commit = original_commit

    conn = captured["wrapper"]
    assert conn._finalized is True
    on_commit_callback.assert_called_once()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_cancelled_commit_still_lands_and_finalizes(db_truncate):
    """RustPgTransactionClient.commit() (hare/backends/rust_pg/client.py) issued the real, top-level
    COMMIT unshielded, unlike sqlite's/asyncpg's own equivalents - a genuine gap, not the
    non-issue an earlier comment in this file assumed: pyo3-async-runtimes 0.29's own
    `Cancellable::poll()` (generic.rs) races the wrapped Rust future against a cancel signal and
    returns early THE MOMENT that signal is observed while the future is still Pending, dropping
    the future right there - it does NOT keep running to completion "regardless of Python-side
    cancellation" (confirmed by reading that exact poll() implementation directly, not assumed).
    A cancellation landing mid-COMMIT could abort the real network round trip after Rust-side
    `.take()` already removed the connection from the Transaction's own inner Mutex, silently
    returning it to the pool via `Object::drop()` with no confirmation the COMMIT was ever
    acknowledged by the server - a live protocol-desync risk for whichever caller acquires that
    same physical connection next - while this wrapper's own `_finalized`/on_commit() bookkeeping
    never gets updated to match.

    Uses the same `_tx`-proxy technique as test_rust_pg_cancelled_release_savepoint_still_lands_
    and_finalizes below - pg.Transaction is a compiled PyO3 type, its methods can't be
    monkeypatched directly."""
    if "hare.dialects.postgresql.drivers.rust_pg" not in type(db_truncate.db()).__module__:
        pytest.skip("rust_pg-specific: proxies pg.Transaction.commit() directly")

    real_landed = asyncio.Event()

    class _DelayedCommitTx:
        def __init__(self, real_tx):
            self._real_tx = real_tx

        async def commit(self):
            result = await self._real_tx.commit()
            real_landed.set()
            await asyncio.sleep(0.3)
            return result

        def __getattr__(self, item):
            return getattr(self._real_tx, item)

    tournament = Tournament(name="cancelled-top-level-commit")
    captured: dict = {}
    on_commit_callback = Mock()

    async def do_transaction():
        async with Transactions.atomic() as conn:
            captured["conn"] = conn
            conn._tx = _DelayedCommitTx(conn._tx)
            Transactions.on_commit(on_commit_callback)
            await tournament.save()
            # commit() runs on __aexit__

    task = asyncio.ensure_future(do_transaction())
    await asyncio.wait_for(real_landed.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    conn = captured["conn"]
    assert conn._finalized is True, "commit() must finalize once the real COMMIT actually lands"
    on_commit_callback.assert_called_once()

    # The real COMMIT already landed - the row it created must be there.
    saved = await Tournament.objects.filter(id=tournament.id).first()
    assert saved is not None

    # The connection isn't left corrupted - a subsequent transaction on it still works normally.
    other = Tournament(name="after-cancelled-top-level-commit")
    async with Transactions.atomic():
        await other.save()
    assert await Tournament.objects.filter(id=other.id).first() is not None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_cancelled_rollback_still_lands_and_finalizes(db_truncate):
    """Companion to test_rust_pg_cancelled_commit_still_lands_and_finalizes above, for the
    top-level ROLLBACK path - same unshielded gap, same fix."""
    if "hare.dialects.postgresql.drivers.rust_pg" not in type(db_truncate.db()).__module__:
        pytest.skip("rust_pg-specific: proxies pg.Transaction.rollback() directly")

    real_landed = asyncio.Event()

    class _DelayedRollbackTx:
        def __init__(self, real_tx):
            self._real_tx = real_tx

        async def rollback(self):
            result = await self._real_tx.rollback()
            real_landed.set()
            await asyncio.sleep(0.3)
            return result

        def __getattr__(self, item):
            return getattr(self._real_tx, item)

    tournament = Tournament(name="should-be-rolled-back")
    captured: dict = {}
    on_rollback_callback = Mock()

    async def do_transaction():
        with pytest.raises(SomeException):
            async with Transactions.atomic() as conn:
                captured["conn"] = conn
                conn._tx = _DelayedRollbackTx(conn._tx)
                Transactions.on_rollback(on_rollback_callback)
                await tournament.save()
                raise SomeException("boom")

    task = asyncio.ensure_future(do_transaction())
    await asyncio.wait_for(real_landed.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    conn = captured["conn"]
    assert conn._finalized is True, "rollback() must finalize once the real ROLLBACK actually lands"
    on_rollback_callback.assert_called_once()

    # The real ROLLBACK already landed - the row it created must be gone.
    assert await Tournament.objects.filter(id=tournament.id).first() is None

    # The connection isn't left corrupted - a subsequent transaction on it still works normally.
    other = Tournament(name="after-cancelled-top-level-rollback")
    async with Transactions.atomic():
        await other.save()
    assert await Tournament.objects.filter(id=other.id).first() is not None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_cancelled_release_savepoint_still_lands_and_finalizes(db_truncate):
    """RustPgTransactionClient.release_savepoint() (hare/backends/rust_pg/client.py) used to issue
    the real RELEASE SAVEPOINT unshielded, unlike asyncpg's/sqlite's own equivalents.
    rust.pg's `Transaction.release()` (rust/pg/src/transaction.rs) never `.take()`s the
    connection the way `commit()`/`rollback()` do, so there's no later `pg.TransactionFinishedError`
    to detect a landed RELEASE SAVEPOINT on a retry either - a cancellation landing mid-call could
    leave the real SQL on the server with this wrapper's own `_finalized` bookkeeping never
    updated.

    A plain `task.cancel()` doesn't reliably land in that narrow gap - once `future_into_py`
    schedules the underlying Rust future, it keeps running on the tokio runtime to completion
    regardless of Python-side cancellation (the same property
    test_rust_pg_transaction_finished_race_does_not_mask_the_real_exception in
    tests/backends/test_postgres.py relies on for commit()/rollback()). `pg.Transaction` is a
    compiled PyO3 extension type - its methods can't be monkeypatched directly the way
    test_asyncpg_cancelled_commit_still_lands_and_finalizes patches asyncpg's own pure-Python
    Transaction class - so this instead swaps the wrapper's own `_tx` attribute (an ordinary
    Python attribute) for a thin proxy that lets the real RELEASE SAVEPOINT land first, then
    introduces an artificial cancellable gap only reachable once it has, mirroring that same
    asyncpg test's own technique."""
    if "hare.dialects.postgresql.drivers.rust_pg" not in type(db_truncate.db()).__module__:
        pytest.skip("rust_pg-specific: proxies pg.Transaction.release() directly")

    real_landed = asyncio.Event()

    class _DelayedReleaseTx:
        def __init__(self, real_tx):
            self._real_tx = real_tx

        async def release(self, name):
            result = await self._real_tx.release(name)
            real_landed.set()
            await asyncio.sleep(0.3)
            return result

        def __getattr__(self, item):
            return getattr(self._real_tx, item)

    tournament = Tournament(name="cancelled-release-savepoint")
    captured: dict = {}

    # RELEASE SAVEPOINT merges the savepoint's work into the ENCLOSING transaction - it never
    # commits independently. Cancelling the whole outer `async with Transactions.atomic():`
    # (as the sibling sqlite/asyncpg commit tests do) would also roll back the outer transaction,
    # undoing the row regardless of whether release_savepoint() itself landed cleanly - that would
    # prove nothing about THIS fix. So only the inner block runs as its own cancellable task; the
    # cancellation is caught right at its boundary, and the outer transaction commits normally
    # afterward - the only way "the row survives" is actually a meaningful assertion here.
    async def do_inner():
        async with Transactions.atomic() as inner:
            captured["inner"] = inner
            inner._tx = _DelayedReleaseTx(inner._tx)
            await tournament.save()

    async def do_transaction():
        async with Transactions.atomic():
            inner_task = asyncio.ensure_future(do_inner())
            await asyncio.wait_for(real_landed.wait(), timeout=2)
            inner_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await inner_task

    await do_transaction()

    inner = captured["inner"]
    assert inner._finalized is True, "release_savepoint() must finalize once the real RELEASE SAVEPOINT actually lands"

    # The real RELEASE SAVEPOINT already landed and the outer transaction committed normally -
    # the row it kept must be there, not dropped by a spurious rollback.
    saved = await Tournament.objects.filter(id=tournament.id).first()
    assert saved is not None

    # The connection isn't left corrupted - a subsequent transaction on it still works normally.
    other = Tournament(name="after-cancelled-release-savepoint")
    async with Transactions.atomic():
        await other.save()
    assert await Tournament.objects.filter(id=other.id).first() is not None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_cancelled_savepoint_rollback_still_lands_and_updates_state(db_truncate):
    """Companion to test_rust_pg_cancelled_release_savepoint_still_lands_and_finalizes above -
    savepoint_rollback()'s ROLLBACK TO had the exact same unshielded gap, reached instead via
    NestedTransactionContext.__aexit__'s automatic rollback path on the automatic `async with
    inner:` exit path. Uses the same `_tx`-proxy technique to let the real ROLLBACK TO SAVEPOINT
    land before introducing a cancellable gap."""
    if "hare.dialects.postgresql.drivers.rust_pg" not in type(db_truncate.db()).__module__:
        pytest.skip("rust_pg-specific: proxies pg.Transaction.rollback_to() directly")

    real_landed = asyncio.Event()

    class _DelayedRollbackTx:
        def __init__(self, real_tx):
            self._real_tx = real_tx

        async def rollback_to(self, name):
            result = await self._real_tx.rollback_to(name)
            real_landed.set()
            await asyncio.sleep(0.3)
            return result

        def __getattr__(self, item):
            return getattr(self._real_tx, item)

    fired = []
    captured: dict = {}

    async def do_transaction():
        async with Transactions.atomic():
            with pytest.raises(SomeException):
                async with Transactions.atomic() as inner:
                    captured["inner"] = inner
                    inner._tx = _DelayedRollbackTx(inner._tx)
                    Transactions.on_commit(lambda: fired.append("should-be-discarded"))
                    await Tournament.objects.create(name="inner-rust-pg-savepoint-rollback")
                    raise SomeException("boom")

    task = asyncio.ensure_future(do_transaction())
    await asyncio.wait_for(real_landed.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    inner = captured["inner"]
    assert inner._finalized is True, "savepoint_rollback() must finalize once the real ROLLBACK TO actually lands"
    assert fired == [], "the discarded on_commit() callback must never fire"

    # The real ROLLBACK TO already ran - the row created inside the savepoint must be gone.
    assert await Tournament.objects.filter(name="inner-rust-pg-savepoint-rollback").first() is None

    # The connection isn't left corrupted - a subsequent transaction on it still works normally.
    other = Tournament(name="after-cancelled-rust-pg-savepoint-rollback")
    async with Transactions.atomic():
        await other.save()
    assert await Tournament.objects.filter(id=other.id).first() is not None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_asyncpg_begin_cancelled_after_landing_rolls_back_before_releasing_pool(
    db_truncate, caplog: pytest.LogCaptureFixture
):
    """begin()'s own shielding re-raises CancelledError to TransactionContextPooled.__aenter__
    even though the real BEGIN it sent already landed server-side - releasing that connection
    straight back to the pool at that point, without first rolling back, made asyncpg's own
    Connection._reset() (which every pool release runs unconditionally) detect the still-open
    transaction and force a ROLLBACK itself - but only after logging it via
    loop.call_exception_handler() as an ERROR-level "Resetting connection with an active
    transaction", confirmed live via caplog. No actual data corruption resulted either way (the
    pool's own safety net already covers it), but an ERROR-level log line firing on an entirely
    routine cancellation is a real operational nuisance - commonly wired to alerting - not just
    log noise to ignore.

    A plain `task.cancel()` while the real network round trip is still in flight does not
    reproduce this directly (asyncpg's own protocol can abort an in-flight BEGIN server-side via
    a genuine Postgres CancelRequest) - the real race is narrower: cancellation landing in the
    gap AFTER Postgres has already acknowledged the BEGIN but BEFORE begin() itself returns. Uses
    the same monkeypatch technique as test_asyncpg_cancelled_commit_still_lands_and_finalizes -
    letting the real Transaction.start() complete first (proving the BEGIN already 100% landed),
    then introducing an artificial cancellable await only reachable once the real work is done."""
    if "hare.dialects.postgresql.drivers.asyncpg" not in type(db_truncate.db()).__module__:
        pytest.skip(
            "asyncpg-specific: monkey-patches asyncpg.transaction.Transaction directly, "
            "which rust_pg never calls (it doesn't use the asyncpg library at all)"
        )

    import asyncpg.transaction

    original_start = asyncpg.transaction.Transaction.start
    real_begin_landed = asyncio.Event()

    async def patched_start(self):
        await original_start(self)
        real_begin_landed.set()
        await asyncio.sleep(0.3)

    asyncpg.transaction.Transaction.start = patched_start

    async def do_transaction():
        async with Transactions.atomic():
            # The BEGIN goes out with the transaction's first statement.
            await Tournament.objects.count()

    try:
        with caplog.at_level("ERROR", logger="asyncio"):
            task = asyncio.ensure_future(do_transaction())
            await asyncio.wait_for(real_begin_landed.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        asyncpg.transaction.Transaction.start = original_start

    assert not any("active transaction" in record.message for record in caplog.records), (
        "releasing a cancelled-but-landed BEGIN back to the pool without rolling back first "
        "must not make asyncpg's own Connection._reset() log an ERROR-level warning"
    )

    # The pooled connection isn't left corrupted (mid-transaction) - a subsequent transaction
    # acquiring it from the pool works normally. Bounded by a timeout so a regression hangs this
    # test instead of the whole suite.
    async def do_next_transaction():
        other = Tournament(name="after-cancelled-begin-asyncpg")
        async with Transactions.atomic():
            await other.save()
        return other

    other = await asyncio.wait_for(do_next_transaction(), timeout=2)
    assert await Tournament.objects.filter(id=other.id).first() is not None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_rust_pg_begin_cancelled_after_landing_rolls_back_and_finalizes(db_truncate):
    """RustPgTransactionContext.__aenter__ had no equivalent of TransactionContextPooled's own
    asyncpg fix (round AH) - unlike asyncpg's separate acquire-then-begin steps, pg.Transaction
    owns its pinned connection for its own lifetime and only gives it back on commit()/
    rollback(), so a cancellation landing after begin() already constructed self.client._tx (see
    begin()'s own shielding docstring) left a real, un-rolled-back transaction sitting on that
    connection forever - permanently shrinking the pool by one slot per occurrence, confirmed
    live via a reproduced pool exhaustion under concurrent begin()+cancel against a small pool.
    Mirrors __aexit__'s own identical defensive rollback (already in this file, for the exit-side
    version of the same race) applied to the entry side instead."""
    if "hare.dialects.postgresql.drivers.rust_pg" not in type(db_truncate.db()).__module__:
        pytest.skip("rust_pg-specific: pg.Transaction is a compiled PyO3 type with its own drop-guard semantics")

    from hare.dialects.postgresql.drivers.rust_pg.client import RustPgTransactionClient

    original_begin = RustPgTransactionClient.begin
    captured: dict = {}
    original_init = RustPgTransactionClient.__init__

    async def patched_begin(self):
        await original_begin(self)
        raise asyncio.CancelledError()

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        captured["wrapper"] = self

    RustPgTransactionClient.begin = patched_begin
    RustPgTransactionClient.__init__ = patched_init

    async def do_transaction():
        async with Transactions.atomic():
            pass

    try:
        with pytest.raises(asyncio.CancelledError):
            await do_transaction()
    finally:
        RustPgTransactionClient.begin = original_begin
        RustPgTransactionClient.__init__ = original_init

    wrapper = captured["wrapper"]
    assert wrapper._tx is not None, "the real BEGIN landed - _tx must have been constructed"
    assert wrapper._finalized is True, "the landed transaction must be rolled back and finalized, not leaked"

    # The pinned connection isn't left corrupted (mid-transaction, permanently unavailable) - a
    # subsequent transaction can still acquire it from the pool. Bounded by a timeout so a
    # regression hangs this test instead of the whole suite.
    async def do_next_transaction():
        other = Tournament(name="after-cancelled-begin-rustpg")
        async with Transactions.atomic():
            await other.save()
        return other

    other = await asyncio.wait_for(do_next_transaction(), timeout=2)
    assert await Tournament.objects.filter(id=other.id).first() is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["reinit", "close_connections"])
async def test_transaction_finishes_when_its_context_replaces_its_connections(replacement):
    """A transaction reset its alias through the context's CURRENT connection handler - after a
    re-init or close_connections() in another task replaced it, leaving the block raised
    "ValueError: <Token ...> was created by a different ContextVar" instead of committing."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    if not DatabaseUnderTest.get_driver(db_url).get_client_classes()[0].features.supports_transactions:
        pytest.skip("The database has no transactions")
    apps = {"models": {"models": ["tests.testmodels"], "default_connection": "models"}}
    async with MultiDatabaseTestContext.open(db_url, ["models"], apps=apps) as ctx:
        config = {"connections": dict(ctx.connections.db_config), "apps": apps}
        await ctx.generate_schemas()
        entered, release = asyncio.Event(), asyncio.Event()

        async def run_transaction() -> None:
            async with Transactions.atomic():
                await Tournament.objects.create(name="inside")
                entered.set()
                await release.wait()

        transaction_task = asyncio.create_task(run_transaction())
        await asyncio.wait_for(entered.wait(), 10)
        if replacement == "reinit":
            replacing_task = asyncio.create_task(ctx.init(config=config))
        else:
            replacing_task = asyncio.create_task(ctx.close_connections())
        await asyncio.sleep(0.2)
        release.set()
        await asyncio.wait_for(transaction_task, 20)
        await asyncio.wait_for(replacing_task, 20)


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_queryset_awaited_inside_a_transaction_is_reusable_after_it(db_truncate):
    """A queryset kept the connection its first await chose - the transaction's - so awaiting the
    same object again after the block ran on the finished transaction."""
    await Tournament.objects.create(name="reused")
    queryset = Tournament.objects.filter(name="reused")
    async with Transactions.atomic():
        assert len(await queryset) == 1
    assert len(await asyncio.wait_for(queryset, 5)) == 1
    assert queryset._db is None


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_queryset_awaited_outside_a_transaction_sees_the_transactions_writes_inside_it(db_truncate):
    """The reverse order: the plain connection kept from a first await bypassed a later
    transaction - on SQLite it waited on the lock that transaction holds (a self-deadlock), on
    Postgres it ran outside the transaction and missed its uncommitted rows."""
    queryset = Tournament.objects.filter(name="new")
    count_query = Tournament.objects.filter(name="new").count()
    values_query = Tournament.objects.filter(name="new").values_list("name", flat=True)
    exists_query = Tournament.objects.filter(name="new").exists()
    assert await queryset == []
    assert await count_query == 0
    assert await values_query == []
    assert await exists_query is False
    async with Transactions.atomic():
        await Tournament.objects.create(name="new")
        assert len(await asyncio.wait_for(queryset, 5)) == 1
        assert await asyncio.wait_for(count_query, 5) == 1
        assert await asyncio.wait_for(values_query, 5) == ["new"]
        assert await asyncio.wait_for(exists_query, 5) is True


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_update_and_delete_queries_reused_inside_a_transaction_write_through_it(db_truncate):
    await Tournament.objects.create(name="target")
    update_query = Tournament.objects.filter(name="target").update(desc="first")
    assert await update_query == 1
    with pytest.raises(RuntimeError):
        async with Transactions.atomic():
            assert await asyncio.wait_for(update_query, 5) == 1
            raise RuntimeError("roll back")
    assert (await Tournament.objects.get(name="target")).desc == "first"

    delete_query = Tournament.objects.filter(name="target").delete()
    with pytest.raises(RuntimeError):
        async with Transactions.atomic():
            assert await asyncio.wait_for(delete_query, 5) == 1
            raise RuntimeError("roll back")
    assert await Tournament.objects.filter(name="target").count() == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_subquery_reused_inside_a_transaction_runs_on_it(db_truncate):
    from hare.query.expressions import Subquery

    tournament_ids = Subquery(Tournament.objects.filter(name="sub").values("id"))
    query = Event.objects.filter(tournament_id__in=tournament_ids)
    assert await query == []
    async with Transactions.atomic():
        tournament = await Tournament.objects.create(name="sub")
        await Event.objects.create(name="event", tournament=tournament)
        assert [event.name for event in await asyncio.wait_for(query, 5)] == ["event"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_queryset_awaited_in_one_task_transaction_does_not_leak_it_to_another_task(db_truncate):
    queryset = Tournament.objects.filter(name="uncommitted")
    awaited_in_transaction, other_task_done = asyncio.Event(), asyncio.Event()

    async def run_transaction() -> None:
        with pytest.raises(RuntimeError):
            async with Transactions.atomic():
                await Tournament.objects.create(name="uncommitted")
                assert len(await queryset) == 1
                awaited_in_transaction.set()
                await asyncio.wait_for(other_task_done.wait(), 10)
                raise RuntimeError("roll back")

    async def read_from_another_task() -> int:
        await asyncio.wait_for(awaited_in_transaction.wait(), 10)
        try:
            return len(await asyncio.wait_for(queryset, 5))
        finally:
            other_task_done.set()

    _, seen_by_other_task = await asyncio.gather(run_transaction(), read_from_another_task())
    assert seen_by_other_task == 0
