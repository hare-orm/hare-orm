import asyncio
import time
from types import SimpleNamespace

import pytest

from hare.dialects.base.client.transaction_lifecycle import transaction_ending as client_module
from hare.dialects.base.client.transaction_lifecycle.transaction_ending import TransactionEnding
from hare.dialects.postgresql.drivers.asyncpg.client import AsyncpgTransactionClient


@pytest.mark.asyncio
async def test_shielded_cancellation_does_not_hang_forever_on_a_never_finishing_operation(monkeypatch):
    """A shielded operation that never completes (dead server, no OS-level timeout) used to make
    _run_shielded_from_cancellation() wait forever after a cancellation - defeating an explicit
    shutdown/timeout the caller asked for. Bounded by SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS
    now - monkeypatched down here so the test itself doesn't take the real default's 30s."""
    monkeypatch.setattr(client_module, "SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS", 0.05)

    landed = False

    def on_landed() -> None:
        nonlocal landed
        landed = True

    async def never_finishes() -> None:
        await asyncio.Event().wait()  # never set - hangs forever

    abandoned_tasks: list[asyncio.Task] = []

    async def run_it() -> None:
        await TransactionEnding.run_shielded_from_cancellation(
            never_finishes(), on_landed=on_landed, on_timeout=abandoned_tasks.append
        )

    task = asyncio.ensure_future(run_it())
    await asyncio.sleep(0.01)
    task.cancel()

    start = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, "cancellation must not be blocked by an operation that never finishes"
    assert landed is False
    # The abandoned operation keeps running by design - stop it so it doesn't outlive the test.
    for abandoned_task in abandoned_tasks:
        abandoned_task.cancel()


@pytest.mark.asyncio
async def test_shielded_cancellation_still_lands_when_the_operation_finishes_within_the_timeout(monkeypatch):
    """The timeout bound must not break the existing, intended behavior: an operation that IS
    still genuinely in flight (just briefly) when cancelled must still be allowed to finish and
    have on_landed() fire, not get abandoned just because a cancellation happened at all."""
    monkeypatch.setattr(client_module, "SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS", 5.0)

    landed = False

    def on_landed() -> None:
        nonlocal landed
        landed = True

    async def finishes_shortly() -> None:
        await asyncio.sleep(0.1)

    async def run_it() -> None:
        await TransactionEnding.run_shielded_from_cancellation(finishes_shortly(), on_landed=on_landed)

    task = asyncio.ensure_future(run_it())
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert landed is True


@pytest.mark.asyncio
async def test_shielded_cancellation_calls_on_timeout_when_the_bound_is_hit(monkeypatch):
    """on_timeout must fire exactly when the wait bound is actually exhausted with the task
    still running - the signal TransactionContextPooled.__aexit__ uses to avoid handing a
    possibly-still-in-use physical connection back to the pool for reuse."""
    monkeypatch.setattr(client_module, "SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS", 0.05)

    timed_out_task: asyncio.Task | None = None

    def on_timeout(abandoned_task: asyncio.Task) -> None:
        nonlocal timed_out_task
        timed_out_task = abandoned_task

    async def never_finishes() -> None:
        await asyncio.Event().wait()

    async def run_it() -> None:
        await TransactionEnding.run_shielded_from_cancellation(
            never_finishes(), on_landed=lambda: None, on_timeout=on_timeout
        )

    task = asyncio.ensure_future(run_it())
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert timed_out_task is not None
    assert not timed_out_task.done()
    timed_out_task.cancel()


@pytest.mark.asyncio
async def test_shielded_cancellation_does_not_call_on_timeout_when_it_lands_in_time(monkeypatch):
    monkeypatch.setattr(client_module, "SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS", 5.0)

    timed_out = False

    def on_timeout() -> None:
        nonlocal timed_out
        timed_out = True

    async def finishes_shortly() -> None:
        await asyncio.sleep(0.1)

    async def run_it() -> None:
        await TransactionEnding.run_shielded_from_cancellation(
            finishes_shortly(), on_landed=lambda: None, on_timeout=on_timeout
        )

    task = asyncio.ensure_future(run_it())
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert timed_out is False


@pytest.mark.asyncio
async def test_pooled_transaction_context_exit_terminates_an_abandoned_connection_before_release(monkeypatch):
    """TransactionContextPooled.__aexit__ used to unconditionally hand the physical connection
    back to the pool for reuse, even when the shielded commit()/rollback() gave up waiting on a
    still-running task - a completely unrelated caller could then acquire and use a connection
    the abandoned task might still be mid-COMMIT against. It must now terminate() the connection
    first whenever _shielded_operation_abandoned was set, so the pool discards it instead of
    reusing it."""
    events: list[str] = []

    fake_connection = SimpleNamespace(terminate=lambda: events.append("terminate"))

    async def fake_release(connection: object) -> None:
        assert connection is fake_connection
        events.append("release")

    fake_pool = SimpleNamespace(release=fake_release)
    fake_client = SimpleNamespace(
        _shielded_operation_abandoned=True,
        _connection=fake_connection,
        _held_pool=fake_pool,
        _parent=SimpleNamespace(_pool=fake_pool, _pool_release=lambda pool, connection: pool.release(connection)),
    )

    await AsyncpgTransactionClient._give_back_transaction_resources(fake_client)  # type: ignore[arg-type]

    assert events == ["terminate", "release"]


@pytest.mark.asyncio
async def test_pooled_transaction_context_exit_releases_normally_when_not_abandoned(monkeypatch):
    """The common case (no timeout ever hit) must be unaffected - no terminate() call, just the
    existing plain release()."""
    events: list[str] = []

    fake_connection = SimpleNamespace(terminate=lambda: events.append("terminate"))

    async def fake_release(connection: object) -> None:
        assert connection is fake_connection
        events.append("release")

    fake_pool = SimpleNamespace(release=fake_release)
    fake_client = SimpleNamespace(
        _shielded_operation_abandoned=False,
        _connection=fake_connection,
        _held_pool=fake_pool,
        _parent=SimpleNamespace(_pool=fake_pool, _pool_release=lambda pool, connection: pool.release(connection)),
    )

    await AsyncpgTransactionClient._give_back_transaction_resources(fake_client)  # type: ignore[arg-type]

    assert events == ["release"]
