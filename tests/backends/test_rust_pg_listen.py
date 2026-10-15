"""Tests for RustPgClient.listen() - LISTEN/NOTIFY on the rust_pg backend (the default
Postgres driver), mirroring tests/backends/test_asyncpg_listen.py's coverage for the same
public contract on the asyncpg backend."""

import asyncio
import contextvars
import gc
import threading

import pytest

from hare import Connections
from hare.contrib.test import requires_features
from hare.dialects.postgresql.drivers.rust_pg.client import RustPgClient


def _rust_pg_client_or_skip() -> RustPgClient:
    client = Connections.get("models")
    if not isinstance(client, RustPgClient):
        pytest.skip("this test targets the rust_pg-specific listen() implementation, not asyncpg's own")
    return client


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_listen_receives_notify_sent_from_a_separate_pooled_connection(db_simple):
    """NOTIFY works today via any pooled connection as plain SQL - execute() needs no
    changes for it. .listen() is the new piece: a dedicated connection whose callback actually
    fires when a DIFFERENT, ordinary pooled connection issues that NOTIFY."""
    client = _rust_pg_client_or_skip()

    received: list[tuple[int, str, str]] = []
    event = asyncio.Event()

    def callback(connection, pid, channel, payload):
        received.append((pid, channel, payload))
        event.set()

    listener = await client.listen("hare_test_rust_pg_listen_channel", callback)
    try:
        await client.execute("NOTIFY hare_test_rust_pg_listen_channel, 'hello-from-notify'")
        await asyncio.wait_for(event.wait(), timeout=5)

        assert len(received) == 1
        pid, channel, payload = received[0]
        assert channel == "hare_test_rust_pg_listen_channel"
        assert payload == "hello-from-notify"
    finally:
        await listener.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_listen_connection_survives_concurrent_pool_usage(db_simple):
    """The listener connection is deliberately never drawn from - or returned to - the
    connection pool: ordinary pooled queries running while it's still open must keep working
    normally, and the listener must keep receiving NOTIFYs throughout."""
    client = _rust_pg_client_or_skip()

    received: list[str] = []
    event = asyncio.Event()

    def callback(connection, pid, channel, payload):
        received.append(payload)
        event.set()

    listener = await client.listen("hare_test_rust_pg_listen_channel_concurrent", callback)
    try:
        for _ in range(5):
            rows_affected, rows = await client.execute("SELECT 1")
            assert rows_affected == 1
            assert rows[0][0] == 1

        event.clear()
        received.clear()
        await client.execute("NOTIFY hare_test_rust_pg_listen_channel_concurrent, 'still-alive'")
        await asyncio.wait_for(event.wait(), timeout=5)
        assert received == ["still-alive"]
    finally:
        await listener.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_listen_passes_itself_as_the_callback_connection_argument(db_simple):
    """callback(connection, pid, channel, payload) - the first argument is the listener object
    itself, matching AsyncpgClient.listen()'s own callback shape."""
    client = _rust_pg_client_or_skip()

    received_connections = []
    event = asyncio.Event()

    def callback(connection, pid, channel, payload):
        received_connections.append(connection)
        event.set()

    listener = await client.listen("hare_test_rust_pg_listen_channel_self", callback)
    try:
        await client.execute("NOTIFY hare_test_rust_pg_listen_channel_self, 'x'")
        await asyncio.wait_for(event.wait(), timeout=5)
        assert received_connections == [listener]
    finally:
        await listener.close()


LISTEN_CALLER_MARKER: contextvars.ContextVar[str | None] = contextvars.ContextVar("LISTEN_CALLER_MARKER", default=None)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_listen_callback_runs_on_the_event_loop_thread_with_caller_context(db_simple):
    """Bug: rust_pg invoked the callback straight from its Tokio worker thread, unlike asyncpg's
    in-loop delivery - anything touching asyncio from it (Event.set(), create_task()) was unsafe
    and could leave the loop asleep until a timeout. The callback must now run on the caller's
    event-loop thread, with the caller's contextvars."""
    client = _rust_pg_client_or_skip()
    LISTEN_CALLER_MARKER.set("listening-caller")
    loop_thread_id = threading.get_ident()

    observed: list[tuple[int, str | None]] = []
    event = asyncio.Event()

    def callback(connection, pid, channel, payload):
        observed.append((threading.get_ident(), LISTEN_CALLER_MARKER.get()))
        event.set()

    listener = await client.listen("hare_test_rust_pg_listen_channel_thread", callback)
    try:
        await client.execute("NOTIFY hare_test_rust_pg_listen_channel_thread, 'x'")
        await asyncio.wait_for(event.wait(), timeout=5)
        assert observed == [(loop_thread_id, "listening-caller")]
    finally:
        await listener.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_listen_returns_a_dedicated_connection_caller_must_close(db_simple):
    """The returned Listener stays open until the caller explicitly closes it, and closing an
    already-closed listener is a no-op, not an error."""
    client = _rust_pg_client_or_skip()

    listener = await client.listen("hare_test_rust_pg_listen_channel_dedicated", lambda *args: None)
    assert listener.is_closed() is False

    await listener.close()
    assert listener.is_closed() is True

    # Idempotent - closing again must not raise.
    await listener.close()
    assert listener.is_closed() is True


async def _find_listener_pid(client: RustPgClient, channel: str) -> int:
    """Looks up the dedicated backend pid a `.listen(channel, ...)` call opened, via
    `pg_stat_activity` - Postgres keeps the last-executed statement text visible there even once
    the connection goes idle after running `LISTEN "channel"`, which is the only query this
    dedicated connection ever runs."""
    rows = await client.execute_dicts(
        f"SELECT pid FROM pg_stat_activity WHERE datname = current_database() AND query ILIKE 'LISTEN %{channel}%'"
    )
    assert len(rows) == 1, f"expected exactly one LISTEN backend for {channel!r}, found {rows}"
    return rows[0]["pid"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_listener_reports_closed_after_its_connection_is_killed(db_simple):
    """Bug: a listener's connection dying underneath it (killed backend, dropped network, ...)
    used to leave `is_closed()` reporting `False` forever, and the callback silently stopped
    firing with no way for the caller to detect it. The background task driving the connection
    must observe the broken notification stream and flip `is_closed()` to `True` on its own."""
    client = _rust_pg_client_or_skip()
    channel = "hare_test_rust_pg_listen_channel_killed"

    received: list[str] = []
    listener = await client.listen(channel, lambda *args: received.append(args[3]))
    try:
        assert listener.is_closed() is False

        listener_pid = await _find_listener_pid(client, channel)
        await client.execute(f"SELECT pg_terminate_backend({listener_pid})")

        for _ in range(100):
            if listener.is_closed():
                break
            await asyncio.sleep(0.05)
        assert listener.is_closed() is True

        # The dead listener must never be mistaken for a live one - a NOTIFY sent afterward
        # (from an unrelated, still-healthy pooled connection) has no one left to deliver it to.
        await client.execute(f"NOTIFY {channel}, 'after-kill'")
        await asyncio.sleep(0.2)
        assert received == []
    finally:
        # Idempotent even though the connection is already dead server-side.
        await listener.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_listener_connection_closes_when_garbage_collected_without_explicit_close(db_simple):
    """Bug: the background task driving a Listener's connection used to hold a STRONG reference
    to the Python `Listener` object for as long as the connection stayed open - which only ever
    happened via an explicit `.close()`. That reference cycle meant `del`+`gc.collect()` on the
    caller's side could never actually free it: the dedicated backend connection leaked forever.
    Fixed by having that background task hold only a weak reference, so ordinary Python
    refcounting reclaims a forgotten Listener - which in turn drops its connection and ends the
    leak - without requiring the caller to ever call `.close()`."""
    client = _rust_pg_client_or_skip()
    channel = "hare_test_rust_pg_listen_channel_gc"

    listener = await client.listen(channel, lambda *args: None)
    listener_pid = await _find_listener_pid(client, channel)

    del listener
    gc.collect()

    for _ in range(100):
        rows = await client.execute_dicts("SELECT 1 FROM pg_stat_activity WHERE pid = $1", [listener_pid])
        if not rows:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail(f"listener backend pid {listener_pid} is still alive after gc.collect() with no references left")
