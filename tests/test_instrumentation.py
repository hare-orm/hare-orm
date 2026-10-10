import asyncio
import gc
import threading
import time
import weakref
from collections import deque

import pytest

from hare import Hare
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.core.config import HareConfig
from hare.core.hare_context import HareContext
from hare.instrumentation.declarations import QueryExecuted
from hare.instrumentation.observers.observer_dispatch import ObserverDispatch
from hare.instrumentation.observers.observers import Observers


@pytest.fixture(autouse=True)
def _clear_observers():
    """The process observers and the kept errors of background observers are process-wide state -
    a test that leaves an observer or an unread error behind would leak it into later tests."""
    Observers.process_observers.clear()
    ObserverDispatch.pending_errors.clear()
    ObserverDispatch.dropped_error_count = 0
    yield
    Observers.process_observers.clear()
    ObserverDispatch.pending_errors.clear()
    ObserverDispatch.dropped_error_count = 0


def notify_query(sql: str, params: list | None = None) -> None:
    """Gives a finished query's event to the observers, as every query-executing call does."""
    Observers.notify(QueryExecuted(sql, params, 1.5, None, "default"))


def record_query(sql: str) -> None:
    """Reports a finished query-executing call - the entry point every client calls."""
    Observers.record_query(sql, None, time.monotonic(), None, "default")


@pytest.mark.asyncio
async def test_observe_and_get_query_event():
    calls = []

    def observer(event):
        calls.append((event.sql, event.parameters, event.duration_ms, event.error, event.connection_alias))

    Observers.observe(QueryExecuted, observer)

    notify_query("SELECT 1")

    assert calls == [("SELECT 1", None, 1.5, None, "default")]


@pytest.mark.asyncio
async def test_plain_function_observer_runs_inline_in_the_task_that_ran_the_query():
    """A plain function gets the event right away, in order, on the calling thread - no worker
    thread and no task in between."""
    caller_thread_id = threading.get_ident()
    seen = []

    def observer(event):
        seen.append((event.sql, threading.get_ident()))

    Observers.observe(QueryExecuted, observer)
    for index in range(5):
        notify_query(f"SELECT {index}")

    assert seen == [(f"SELECT {index}", caller_thread_id) for index in range(5)]


@pytest.mark.asyncio
async def test_unobserve_stops_further_calls():
    calls = []

    def observer(event):
        calls.append(event.sql)

    Observers.observe(QueryExecuted, observer)
    Observers.unobserve(QueryExecuted, observer)

    notify_query("SELECT 1")

    assert calls == []


def test_unobserve_is_idempotent():
    """Unobserving an observer that was never registered (or already removed) doesn't raise."""
    Observers.unobserve(QueryExecuted, lambda event: None)


@pytest.mark.asyncio
async def test_a_raising_observer_is_logged_and_isolated(caplog):
    def bad_observer(event):
        raise RuntimeError("observer exploded")

    calls = []

    def observer(event):
        calls.append(event.sql)

    Observers.observe(QueryExecuted, bad_observer)
    Observers.observe(QueryExecuted, observer)

    with caplog.at_level("ERROR", logger="hare"):
        notify_query("SELECT 1")

    assert calls == ["SELECT 1"]
    assert any("raised" in message for message in caplog.messages)


@pytest.mark.asyncio
async def test_every_registered_observer_runs():
    calls = []
    Observers.observe(QueryExecuted, lambda event: calls.append(1))
    Observers.observe(QueryExecuted, lambda event: calls.append(2))

    notify_query("SELECT 1")

    assert sorted(calls) == [1, 2]


@pytest.mark.asyncio
async def test_async_observer_runs_in_the_background():
    """An ``async def`` observer never delays the query - it runs once the caller yields, and
    ``wait_for_pending()`` lets it finish."""
    calls = []

    async def async_observer(event):
        await asyncio.sleep(0.05)
        calls.append(event.sql)

    Observers.observe(QueryExecuted, async_observer)

    started = time.monotonic()
    notify_query("SELECT 1")
    assert time.monotonic() - started < 0.04
    assert calls == []

    await Observers.wait_for_pending()
    assert calls == ["SELECT 1"]


@pytest.mark.asyncio
async def test_async_callable_instance_observer_is_awaited_not_dropped(recwarn):
    """An instance with ``async def __call__`` is an async observer like an ``async def``
    function - its coroutine is awaited, never dropped with a "never awaited" warning."""

    class AsyncCallableObserver:
        def __init__(self):
            self.called_with = None

        async def __call__(self, event):
            self.called_with = event.sql

    observer = AsyncCallableObserver()
    Observers.observe(QueryExecuted, observer)

    notify_query("SELECT 1")
    await Observers.wait_for_pending()

    assert observer.called_with == "SELECT 1"
    assert not any("was never awaited" in str(warning.message) for warning in recwarn.list)


@pytest.mark.asyncio
async def test_async_observers_see_events_in_the_order_they_happened():
    seen = []

    async def observer(event):
        # Without ordering, the first dispatch sleeps longest and would append last.
        await asyncio.sleep(0.02 if event.sql == "SELECT 0" else 0)
        seen.append(event.sql)

    Observers.observe(QueryExecuted, observer)
    for index in range(5):
        notify_query(f"SELECT {index}")
    await Observers.wait_for_pending()

    assert seen == [f"SELECT {index}" for index in range(5)]


@pytest.mark.asyncio
async def test_record_query_does_not_wait_for_a_slow_async_observer():
    """``record_query()`` runs after every query - a slow async observer must not add its own
    duration to the query."""

    async def slow_observer(event):
        await asyncio.sleep(0.3)

    Observers.observe(QueryExecuted, slow_observer)

    started = time.monotonic()
    record_query("SELECT 1")
    elapsed = time.monotonic() - started

    assert elapsed < 0.1, f"record_query() blocked for {elapsed}s waiting on an observer"
    await Observers.wait_for_pending()


@pytest.mark.asyncio
async def test_wait_for_pending_is_a_noop_with_nothing_pending():
    await Observers.wait_for_pending()


@pytest.mark.asyncio
async def test_wait_for_pending_reraises_a_single_observer_exception(caplog):
    """A strict-mode async observer (a test's own check) has its exception reach a caller that
    synchronizes with ``wait_for_pending()`` - logged when it happened, and re-raised once."""

    async def bad_observer(event):
        raise RuntimeError("strict observer exploded")

    Observers.observe(QueryExecuted, bad_observer)

    with caplog.at_level("ERROR", logger="hare"):
        notify_query("SELECT 1")
        with pytest.raises(RuntimeError, match="strict observer exploded"):
            await Observers.wait_for_pending()

    # Draining again doesn't re-raise the same exception a second time.
    await Observers.wait_for_pending()


@pytest.mark.asyncio
async def test_wait_for_pending_groups_multiple_observer_exceptions():
    async def bad_observer_one(event):
        raise RuntimeError("first observer exploded")

    async def bad_observer_two(event):
        raise ValueError("second observer exploded")

    Observers.observe(QueryExecuted, bad_observer_one)
    Observers.observe(QueryExecuted, bad_observer_two)

    notify_query("SELECT 1")
    with pytest.raises(ExceptionGroup) as excinfo:
        await Observers.wait_for_pending()

    assert len(excinfo.value.exceptions) == 2


@pytest.mark.asyncio
async def test_record_query_uses_the_class_level_slow_query_threshold(caplog, monkeypatch):
    """``record_query()`` reads ``slow_query_threshold_ms`` on every call - an override takes
    effect."""
    monkeypatch.setattr(Observers, "slow_query_threshold_ms", -1.0)

    with caplog.at_level("DEBUG", logger="hare.db_client"):
        record_query("SELECT 1")

    assert any("Slow query" in message for message in caplog.messages)


def test_record_query_without_observers_builds_no_event(monkeypatch):
    """``record_query()`` runs after every query - with no observer it doesn't even build the
    event, yet still logs a slow query."""

    def fail(*args, **kwargs):
        raise AssertionError("an event was given out although nothing observes it")

    monkeypatch.setattr(Observers, "notify", fail)
    assert Observers.record_query("SELECT 1", None, time.monotonic(), None, "default") is None


@pytest.mark.asyncio
async def test_hare_init_overrides_slow_query_threshold(caplog):
    """``Hare.init(slow_query_threshold_ms=...)`` changes the threshold real queries are logged by."""
    async with HareContext() as ctx:
        await Hare.init(
            HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": ["tests.testmodels"]}),
            slow_query_threshold_ms=0.0,
        )
        assert Observers.slow_query_threshold_ms == 0.0

        with caplog.at_level("DEBUG", logger="hare.db_client"):
            await ctx.get_connection().execute("SELECT 1")

        assert any("Slow query" in message for message in caplog.messages)


@pytest.mark.asyncio
async def test_real_queries_reach_the_observer_with_their_connection():
    from tests.testmodels import IntFields

    async with hare_test_context(["tests.testmodels"]):
        events = []
        with Observers.observing(QueryExecuted, events.append):
            await IntFields.objects.filter(intnum=-1).count()
        connection_alias = IntFields.get_connection().connection_alias

    assert len(events) == 1
    assert "SELECT COUNT" in events[0].sql.upper()
    assert events[0].connection_alias == connection_alias
    assert events[0].error is None


@pytest.mark.asyncio
async def test_queries_an_observer_runs_itself_report_no_events():
    """An audit observer that writes to the database observes the user's query once - its own
    INSERT reporting events again would re-trigger it forever."""
    from tests.testmodels import IntFields

    async with hare_test_context(["tests.testmodels"]):
        observed_sql = []

        async def audit_observer(event):
            observed_sql.append(event.sql)
            await IntFields.objects.create(intnum=len(observed_sql))

        Observers.observe(QueryExecuted, audit_observer)
        try:
            await IntFields.objects.filter(intnum=-1).count()
            await Observers.wait_for_pending()
            await asyncio.sleep(0.1)
            await Observers.wait_for_pending()
        finally:
            Observers.unobserve(QueryExecuted, audit_observer)

        assert len(observed_sql) == 1
        assert await IntFields.objects.all().count() == 1


@pytest.mark.asyncio
async def test_kept_observer_errors_are_bounded_and_counted(monkeypatch):
    monkeypatch.setattr(ObserverDispatch, "pending_errors", deque(maxlen=3))

    async def bad_observer(event):
        raise RuntimeError(event.sql)

    Observers.observe(QueryExecuted, bad_observer)
    for index in range(5):
        notify_query(f"SELECT {index}")
        await asyncio.wait(list(ObserverDispatch.pending_tasks))

    assert [str(error) for error in ObserverDispatch.pending_errors] == ["SELECT 2", "SELECT 3", "SELECT 4"]
    with pytest.raises(ExceptionGroup, match="2 older failures") as excinfo:
        await Observers.wait_for_pending()
    assert len(excinfo.value.exceptions) == 3
    assert ObserverDispatch.dropped_error_count == 0
    await Observers.wait_for_pending()


@pytest.mark.asyncio
async def test_kept_observer_error_does_not_keep_the_observer_locals_alive():
    class Payload:
        pass

    payload_references = []

    async def bad_observer(event):
        payload = Payload()
        payload_references.append(weakref.ref(payload))
        raise RuntimeError("observer failed")

    Observers.observe(QueryExecuted, bad_observer)
    notify_query("SELECT 1", ["large parameter"])
    await asyncio.wait(list(ObserverDispatch.pending_tasks))
    gc.collect()

    assert payload_references[0]() is None
    with pytest.raises(RuntimeError, match="observer failed"):
        await Observers.wait_for_pending()


@pytest.mark.asyncio
async def test_dispatches_past_the_backlog_limit_are_dropped_with_a_warning(monkeypatch, caplog):
    monkeypatch.setattr("hare.instrumentation.observers.observer_dispatch.MAX_PENDING_OBSERVER_DISPATCHES", 3)
    release = asyncio.Event()
    calls = []

    async def stuck_observer(event):
        await release.wait()
        calls.append(event.sql)

    Observers.observe(QueryExecuted, stuck_observer)
    with caplog.at_level("WARNING", logger="hare"):
        for index in range(5):
            notify_query(f"SELECT {index}")
        assert len(ObserverDispatch.pending_tasks) == 3
        release.set()
        await Observers.wait_for_pending()
        notify_query("SELECT after")
        await Observers.wait_for_pending()

    assert calls == ["SELECT 0", "SELECT 1", "SELECT 2", "SELECT after"]
    assert any("backlog is full" in message for message in caplog.messages)
    assert any("2 dispatches were dropped" in message for message in caplog.messages)


@pytest.mark.asyncio
async def test_wait_for_pending_cancels_a_stuck_observer_after_the_timeout(caplog):
    cancelled = []

    async def stuck_observer(event):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(event.sql)
            raise

    Observers.observe(QueryExecuted, stuck_observer)
    notify_query("SELECT 1")
    notify_query("SELECT 2")

    with caplog.at_level("WARNING", logger="hare"):
        await asyncio.wait_for(Observers.wait_for_pending(timeout_seconds=0.1), 5)

    assert cancelled == ["SELECT 1"]
    assert ObserverDispatch.pending_tasks == set()
    assert any("still running after" in message for message in caplog.messages)


@pytest.mark.asyncio
async def test_close_connections_does_not_wait_forever_for_a_stuck_observer(monkeypatch):
    monkeypatch.setattr("hare.core.hare_context.OBSERVER_SHUTDOWN_WAIT_TIMEOUT_SECONDS", 0.1)

    async def stuck_observer(event):
        await asyncio.Event().wait()

    async with HareContext():
        await Hare.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": ["tests.testmodels"]}))
        Observers.observe(QueryExecuted, stuck_observer)
        notify_query("SELECT 1")
        await asyncio.wait_for(Hare.close_connections(), 5)

    assert ObserverDispatch.pending_tasks == set()


@pytest.mark.parametrize("timeout_seconds", [0, -1, float("nan"), float("inf"), True, "1"])
@pytest.mark.asyncio
async def test_wait_for_pending_rejects_an_invalid_timeout(timeout_seconds):
    with pytest.raises(ValueError, match="timeout_seconds"):
        await Observers.wait_for_pending(timeout_seconds=timeout_seconds)


@pytest.mark.asyncio
async def test_observing_reaches_only_the_block_and_the_tasks_it_starts():
    seen = []

    async def run_in_task():
        notify_query("SELECT in task")

    with Observers.observing(QueryExecuted, lambda event: seen.append(event.sql)):
        notify_query("SELECT inside")
        await asyncio.create_task(run_in_task())
    notify_query("SELECT outside")

    assert seen == ["SELECT inside", "SELECT in task"]


@pytest.mark.asyncio
async def test_context_observer_gets_events_only_while_its_context_is_current():
    from tests.testmodels import IntFields

    seen = []
    async with hare_test_context(["tests.testmodels"]) as ctx:
        ctx.observe(QueryExecuted, lambda event: seen.append(event.sql))
        await IntFields.objects.filter(intnum=-1).count()
    async with hare_test_context(["tests.testmodels"]):
        await IntFields.objects.filter(intnum=-1).count()

    assert len(seen) == 1
