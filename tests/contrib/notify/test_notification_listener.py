"""Tests for PostgresqlClient.notify(), Capabilities.supports_listen_notify and
hare.contrib.notify.NotificationListener."""

import asyncio
import contextlib
from unittest import mock

import pytest

from hare import Connections
from hare.contrib.notify import NotificationListener
from hare.contrib.test import requires_features
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.transactions.transactions import Transactions
from tests.utils.waiting import WAIT_DEADLINE_SECONDS, wait_until

CONNECTION_ALIAS = "models"


class Collector:
    """Records received payloads and lets a test await the next one."""

    def __init__(self) -> None:
        self.payloads: list[str] = []
        self.received = asyncio.Event()

    def __call__(self, payload: str) -> None:
        self.payloads.append(payload)
        self.received.set()

    async def wait(self, timeout: float = WAIT_DEADLINE_SECONDS) -> None:
        await asyncio.wait_for(self.received.wait(), timeout=timeout)
        self.received.clear()


@contextlib.contextmanager
def fake_notification_channels(listen):
    """The client given notification channels whose connections ``listen`` opens."""
    client = Connections.get(CONNECTION_ALIAS)
    with (
        mock.patch.object(client, "features", client.features.replace(supports_listen_notify=True)),
        mock.patch.object(client, "listen", listen, create=True),
    ):
        yield


async def listen_backend_pids(channel: str) -> list[int]:
    """PIDs of backends whose last statement was a LISTEN on ``channel``."""
    _, rows = await Connections.get(CONNECTION_ALIAS).execute(
        "SELECT pid FROM pg_stat_activity WHERE query ILIKE $1 AND pid <> pg_backend_pid()",
        [f"%LISTEN%{channel}%"],
    )
    return [row[0] for row in rows]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_postgres_capabilities_support_listen_notify(db_simple):
    assert Connections.get(CONNECTION_ALIAS).features.supports_listen_notify is True


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_sqlite_capabilities_do_not_support_listen_notify(db_simple):
    client = Connections.get(CONNECTION_ALIAS)
    assert client.features.supports_listen_notify is False
    assert not hasattr(client, "notify")


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_start_on_sqlite_is_refused(db_simple):
    listener = NotificationListener(CONNECTION_ALIAS, "hare_test_notify_sqlite", Collector())
    with pytest.raises(UnSupportedError, match="sqlite has no notification channels"):
        await listener.start()
    with pytest.raises(UnSupportedError, match="sqlite has no notification channels"):
        await listener.run()
    assert listener._run_task is None
    assert not listener.is_listening
    await listener.stop()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_notify_reaches_the_listener(db_simple):
    channel = "hare_test_notify_basic"
    collector = Collector()
    async with NotificationListener(CONNECTION_ALIAS, channel, collector) as listener:
        assert listener.is_listening
        await Connections.get(CONNECTION_ALIAS).notify(channel, "hello")
        await collector.wait()
    assert collector.payloads == ["hello"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_notify_payload_is_parameterized_not_interpolated(db_simple):
    channel = "hare_test_notify_quoting"
    payload = 'it\'s a "quoted"; SELECT 1 --'
    collector = Collector()
    async with NotificationListener(CONNECTION_ALIAS, channel, collector):
        await Connections.get(CONNECTION_ALIAS).notify(channel, payload)
        await collector.wait()
    assert collector.payloads == [payload]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_async_callback_is_supported(db_simple):
    channel = "hare_test_notify_async_callback"
    received: list[str] = []
    done = asyncio.Event()

    async def callback(payload: str) -> None:
        await asyncio.sleep(0)
        received.append(payload)
        done.set()

    async with NotificationListener(CONNECTION_ALIAS, channel, callback):
        await Connections.get(CONNECTION_ALIAS).notify(channel, "async")
        await asyncio.wait_for(done.wait(), timeout=5)
    assert received == ["async"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_failing_callback_does_not_kill_the_listener(db_simple):
    channel = "hare_test_notify_failing_callback"
    collector = Collector()

    def callback(payload: str) -> None:
        if payload == "boom":
            raise RuntimeError("callback failure")
        collector(payload)

    async with NotificationListener(CONNECTION_ALIAS, channel, callback) as listener:
        await Connections.get(CONNECTION_ALIAS).notify(channel, "boom")
        await Connections.get(CONNECTION_ALIAS).notify(channel, "after")
        await collector.wait()
        assert listener.is_listening
    assert collector.payloads == ["after"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_notify_in_rolled_back_transaction_is_not_delivered(db_simple):
    channel = "hare_test_notify_rollback"
    collector = Collector()
    async with NotificationListener(CONNECTION_ALIAS, channel, collector):
        with pytest.raises(RuntimeError, match="roll back"):
            async with Transactions.atomic(CONNECTION_ALIAS) as connection:
                await connection.notify(channel, "rolled-back")
                raise RuntimeError("roll back")
        # A channel's notifications arrive in order - once the one sent after the rollback arrived alone,
        # the rolled back one was never sent.
        await Connections.get(CONNECTION_ALIAS).notify(channel, "control")
        await collector.wait()
    assert collector.payloads == ["control"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_notify_in_committed_transaction_is_delivered_only_after_commit(db_simple):
    channel = "hare_test_notify_commit"
    collector = Collector()
    outside_client = Connections.current().create_independent(CONNECTION_ALIAS)
    try:
        async with NotificationListener(CONNECTION_ALIAS, channel, collector):
            async with Transactions.atomic(CONNECTION_ALIAS) as connection:
                await connection.notify(channel, "committed")
                # Sent on another connection, after the transaction's notification - it arrives while
                # the other waits for the commit.
                await outside_client.notify(channel, "outside")
                await collector.wait()
                assert collector.payloads == ["outside"]
            await collector.wait()
    finally:
        await outside_client.close()
    assert collector.payloads == ["outside", "committed"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_listener_reconnects_after_its_backend_is_terminated(db_simple):
    channel = "hare_test_notify_reconnect"
    collector = Collector()
    client = Connections.get(CONNECTION_ALIAS)
    async with NotificationListener(CONNECTION_ALIAS, channel, collector, backoff=0.05) as listener:
        original_pids = await listen_backend_pids(channel)
        assert len(original_pids) == 1
        await client.execute("SELECT pg_terminate_backend($1)", [original_pids[0]])

        new_pids: list[int] = []

        async def has_new_backend() -> bool:
            new_pids[:] = await listen_backend_pids(channel)
            return bool(new_pids) and new_pids != original_pids and listener.is_listening

        await wait_until(has_new_backend)
        assert original_pids[0] not in new_pids

        await client.notify(channel, "after-reconnect")
        await collector.wait()
    assert collector.payloads == ["after-reconnect"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_stop_cleans_up_task_connection_and_callback_tasks(db_simple):
    channel = "hare_test_notify_stop"
    entered = asyncio.Event()
    never = asyncio.Event()

    async def hanging_callback(payload: str) -> None:
        entered.set()
        await never.wait()

    listener = NotificationListener(CONNECTION_ALIAS, channel, hanging_callback)
    await listener.start()
    run_task = listener._run_task
    assert run_task is not None
    await Connections.get(CONNECTION_ALIAS).notify(channel, "x")
    await asyncio.wait_for(entered.wait(), timeout=5)
    callback_tasks = list(listener._callback_tasks)
    assert len(callback_tasks) == 1

    await listener.stop()

    assert run_task.done()
    assert all(task.done() for task in callback_tasks)
    assert listener._callback_tasks == set()
    assert not listener.is_listening

    async def has_no_backend() -> bool:
        return not await listen_backend_pids(channel)

    await wait_until(has_no_backend)
    await listener.stop()  # idempotent


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_start_is_idempotent(db_simple):
    listener = NotificationListener(CONNECTION_ALIAS, "hare_test_notify_idempotent", Collector())
    await listener.start()
    first_task = listener._run_task
    await listener.start()
    assert listener._run_task is first_task
    await listener.stop()


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_gives_up_after_reconnect_attempts_with_backoff(db_simple):
    """Runs on SQLite with fake notification channels whose connection is dead on arrival."""

    class DeadOnArrivalConnection:
        def is_closed(self) -> bool:
            return True

        async def close(self) -> None:
            return None

    connect_count = 0

    async def fake_listen(channel: str, callback: object) -> DeadOnArrivalConnection:
        nonlocal connect_count
        connect_count += 1
        return DeadOnArrivalConnection()

    sleeps: list[float] = []

    async def fast_pause(self: NotificationListener, delay: float) -> None:
        sleeps.append(delay)

    listener = NotificationListener(
        CONNECTION_ALIAS, "dead_on_arrival", Collector(), reconnect_attempts=3, backoff=(0.1, 0.2)
    )
    with fake_notification_channels(fake_listen), mock.patch.object(NotificationListener, "pause", fast_pause):
        await listener.run()

    assert connect_count == 4
    assert sleeps == [0.1, 0.2, 0.2]


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_listen_setup_errors_are_retried(db_simple):
    attempts = 0

    async def failing_listen(channel: str, callback: object) -> None:
        nonlocal attempts
        attempts += 1
        raise ConnectionRefusedError("down")

    async def fast_pause(self: NotificationListener, delay: float) -> None:
        return None

    listener = NotificationListener(CONNECTION_ALIAS, "failing", Collector(), reconnect_attempts=2)
    with fake_notification_channels(failing_listen), mock.patch.object(NotificationListener, "pause", fast_pause):
        await listener.run()
    assert attempts == 3


@pytest.mark.database_independent
def test_exponential_backoff_is_clamped():
    listener = NotificationListener(CONNECTION_ALIAS, "c", Collector(), backoff=0.5, max_backoff_seconds=3)
    assert [listener._get_backoff_delay(attempt) for attempt in range(5)] == [0.5, 1.0, 2.0, 3, 3]
    unbounded = NotificationListener(CONNECTION_ALIAS, "c", Collector(), max_backoff_seconds=None)
    assert unbounded._get_backoff_delay(10_000) == 0.5 * 2**32


@pytest.mark.database_independent
@pytest.mark.parametrize(
    "options",
    [
        {"reconnect_attempts": -1},
        {"reconnect_attempts": True},
        {"reconnect_attempts": 1.5},
        {"backoff": -0.1},
        {"backoff": float("inf")},
        {"backoff": ()},
        {"backoff": (0.1, -1)},
        {"backoff": "1"},
        {"max_backoff_seconds": 0},
        {"max_backoff_seconds": float("nan")},
    ],
)
def test_invalid_options_raise(options):
    with pytest.raises(ConfigurationError):
        NotificationListener(CONNECTION_ALIAS, "c", Collector(), **options)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_listener_started_inside_a_transaction_listens_on_the_shared_client(db_simple):
    channel = "hare_test_notify_started_in_transaction"
    collector = Collector()
    listener = NotificationListener(CONNECTION_ALIAS, channel, collector, reconnect_attempts=0)
    try:
        async with Transactions.atomic():
            await listener.start()
        assert listener.is_listening
        await Connections.get(CONNECTION_ALIAS).notify(channel, "after the transaction")
        await collector.wait()
    finally:
        await listener.stop()
    assert collector.payloads == ["after the transaction"]


class HealthyConnection:
    def __init__(self) -> None:
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_stop_ends_a_run_awaited_directly(db_simple):
    """stop() makes a run() the caller awaits itself return - it doesn't cancel the caller's task."""
    connection = HealthyConnection()

    async def fake_listen(channel: str, callback: object) -> HealthyConnection:
        return connection

    events: list[str] = []
    listener = NotificationListener(CONNECTION_ALIAS, "owned_by_caller", Collector())

    async def caller() -> None:
        await listener.run()
        events.append("after run")

    with fake_notification_channels(fake_listen):
        caller_task = asyncio.create_task(caller())
        await wait_until(lambda: listener.is_listening)
        await listener.stop()
        await asyncio.wait_for(caller_task, timeout=5)
    assert events == ["after run"]
    assert connection.closed


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_stop_does_not_wait_out_the_backoff(db_simple):
    attempts = 0

    async def failing_listen(channel: str, callback: object) -> None:
        nonlocal attempts
        attempts += 1
        raise ConnectionRefusedError("down")

    # An hour of backoff - a stop waiting it out never ends within the deadline of the wait.
    listener = NotificationListener(CONNECTION_ALIAS, "slow_backoff", Collector(), backoff=3600.0)
    with fake_notification_channels(failing_listen):
        run_task = asyncio.create_task(listener.run())
        await wait_until(lambda: attempts > 0)
        await asyncio.wait_for(listener.stop(), timeout=WAIT_DEADLINE_SECONDS)
        await asyncio.wait_for(run_task, timeout=WAIT_DEADLINE_SECONDS)
    assert attempts == 1


@pytest.mark.database_independent
@pytest.mark.parametrize("channel", ["", None, "a\x00b"])
def test_a_channel_no_listen_takes_is_refused(channel):
    """start() retried such a channel forever - no reconnect makes it a channel."""
    with pytest.raises(ConfigurationError, match="channel must be a non-empty text"):
        NotificationListener(CONNECTION_ALIAS, channel, Collector())
