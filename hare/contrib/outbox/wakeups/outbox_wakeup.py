from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterable
from functools import partial
from threading import RLock
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

import orjson

from hare.contrib.outbox.outbox_release import OutboxRelease
from hare.contrib.outbox.wakeups.constants import (
    WAKEUP_HEALTHY_SECONDS,
    WAKEUP_MAX_RECONNECT_ATTEMPTS,
    WAKEUP_RECONNECT_BACKOFF_SECONDS,
)
from hare.core.log import logger
from hare.core.resource_completion import ResourceCompletion
from hare.dialects.base.client.transaction_lifecycle.transaction_callbacks import TransactionCallbacks
from hare.instrumentation.capture.change_capturing import ChangeCapturing

if TYPE_CHECKING:
    from concurrent.futures import Future

    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.client.transaction_client import TransactionClient
    from hare.dialects.base.transactions.savepoints.savepoint_span import SavepointSpan

#: What a relay gets from a wakeup - the topics of the events written, empty when not known.
WakeupCallback = Callable[[frozenset[str]], None]
#: The topics of the events a transaction wrote, by the savepoint they were written in - None at the
#: transaction's own level.
SavepointTopics = dict["SavepointSpan | None", set[str]]


class OutboxWakeup:
    """Wakes the relays once outbox events are written, instead of their waiting for the next poll.
    One object is given both to the outbox model (``Meta.outbox_wakeup``, or
    ``enqueue(wakeup=...)``) and to the relays. The signals of one transaction are joined - one per
    topic, sent once it commits; a signal is never sent for a transaction rolled back. A lost signal
    is harmless: the relays poll anyway.

    A subclass sends a signal (``signal()``) and listens for them (``listen()``); a subscription
    that fails is retried with backoff and, after ``WAKEUP_MAX_RECONNECT_ATTEMPTS`` failures in a
    row, given up with an ERROR - the relays keep polling.
    """

    def __deepcopy__(self, memo: dict[int, object]) -> OutboxWakeup:
        """Keep the shared signal service when copying a model's declarative options.

        Models and relays must refer to the same subscriptions, tasks and transaction
        signals. These runtime resources are not declarations to duplicate.
        """
        memo[id(self)] = self
        return self

    def __init__(self) -> None:
        #: The topics of each open transaction by savepoint, used to join or deduplicate signals.
        #: Topics of a savepoint that rolls back are dropped.
        self.pending_topics: WeakKeyDictionary[TransactionClient, SavepointTopics] = WeakKeyDictionary()
        #: The relays' callbacks, each with the event loop it is called on.
        self.subscriptions: list[tuple[WakeupCallback, asyncio.AbstractEventLoop]] = []
        #: The task listening for signals while a relay is subscribed.
        self.listen_task: asyncio.Task[None] | None = None
        self._subscriptions_lock = RLock()
        self._stop_future: Future[None] | None = None

    def raise_if_unsupported(self, connection: DatabaseClient) -> None:
        """Refuses a connection the wakeup can't signal the events of - checked before they are written.

        Args:
            connection: The connection the events are written on.
        """

    async def signal_written(self, connection: DatabaseClient, topics: Iterable[str]) -> None:
        """Signals events written on ``connection`` - once its transaction commits, at once outside one.

        Args:
            connection: The connection the events were written on.
            topics: Their topics.
        """
        if not ChangeCapturing.is_in_transaction(connection):
            await self.signal_safely(frozenset(topics))
            return
        transaction, savepoint_span, topics_by_savepoint = self._get_topic_scope(connection)
        pending = self._register_topic_bucket(
            connection, transaction, savepoint_span, topics_by_savepoint, signal_on_commit=True
        )
        pending.update(topics)

    def _get_topic_scope(
        self, connection: TransactionClient
    ) -> tuple[TransactionClient, SavepointSpan | None, SavepointTopics]:
        """The top-level transaction, active savepoint and their topic storage."""
        transaction = connection._get_top_level_transaction()
        savepoint_span = TransactionCallbacks.get_registration_span(connection)
        return transaction, savepoint_span, self.pending_topics.setdefault(transaction, {})

    def _register_topic_bucket(
        self,
        connection: TransactionClient,
        transaction: TransactionClient,
        savepoint_span: SavepointSpan | None,
        topics_by_savepoint: SavepointTopics,
        *,
        signal_on_commit: bool,
    ) -> set[str]:
        """Register cleanup for the topics first written in this transaction or savepoint."""
        pending = topics_by_savepoint.get(savepoint_span)
        if pending is not None:
            return pending
        pending = topics_by_savepoint[savepoint_span] = set()
        if signal_on_commit:
            TransactionCallbacks.add_on_commit_callback(
                connection, partial(self.signal_pending, transaction), is_user_callback=False
            )
        TransactionCallbacks.add_on_rollback_callback(
            connection, partial(self.drop_pending, transaction, savepoint_span), is_user_callback=False
        )
        return pending

    async def signal_pending(self, transaction: TransactionClient) -> None:
        """Signals the topics a committed transaction wrote - once, joined, whichever of its
        savepoints' callbacks runs first.

        Args:
            transaction: The top-level transaction.
        """
        pending_by_savepoint = self.pending_topics.pop(transaction, None)
        topics = set().union(*pending_by_savepoint.values()) if pending_by_savepoint else set()
        if topics:
            await self.signal_safely(frozenset(topics))

    def drop_pending(self, transaction: TransactionClient, savepoint_span: SavepointSpan | None) -> None:
        """Forgets the topics written in a savepoint, or a transaction, that rolled back.

        Args:
            transaction: The top-level transaction.
            savepoint_span: The savepoint, None for the transaction's own level.
        """
        pending_by_savepoint = self.pending_topics.get(transaction)
        if pending_by_savepoint is not None:
            pending_by_savepoint.pop(savepoint_span, None)

    async def signal_safely(self, topics: frozenset[str]) -> None:
        """``signal()``, a failure logged - the relays' polling delivers the events anyway.

        Args:
            topics: The topics.
        """
        try:
            await self.signal(topics)
        except Exception:
            logger.exception("%s: signalling outbox events of %s failed", type(self).__name__, sorted(topics))

    async def signal(self, topics: frozenset[str]) -> None:
        """Sends a signal to the relays.

        Args:
            topics: The topics of the events written.
        """
        raise NotImplementedError

    @staticmethod
    def encode_topics(topics: frozenset[str]) -> bytes:
        """Encodes broker signals as a JSON array, preserving delimiters and Unicode in topic names.

        Args:
            topics: The topics carried by one signal; empty means the topics aren't known.

        Returns:
            UTF-8 JSON bytes shared by Redis, Kafka and RabbitMQ wakeups.
        """
        return orjson.dumps(sorted(topics))

    def dispatch_payload(self, payload: str | bytes) -> None:
        """Decodes a broker signal and gives its exact topic names to subscribed relays.

        Args:
            payload: The JSON array written by ``encode_topics()``. Invalid JSON raises, so the
                listener follows its normal failure and reconnect handling.
        """
        self.dispatch(frozenset(orjson.loads(payload)))

    async def subscribe(self, callback: WakeupCallback, connection: str | DatabaseClient) -> None:
        """Calls ``callback`` on every signal from now on - a relay's subscription.

        Args:
            callback: The relay's callback - called on the event loop's thread, never awaited.
            connection: The relay's selected client, or a connection alias for standalone use.
        """
        loop = asyncio.get_running_loop()
        while True:
            with self._subscriptions_lock:
                stopping = self._stop_future
                if stopping is None:
                    self.subscriptions.append((callback, loop))
                    if self.listen_task is None:
                        self.listen_task = loop.create_task(self.keep_listening(partial(self.listen, connection)))
                    return
            # A new listener must not reuse a transport whose previous listener is closing it.
            await ResourceCompletion.wait(asyncio.wrap_future(stopping))

    async def unsubscribe(self, callback: WakeupCallback) -> None:
        """Stops calling ``callback`` - the listening stops with the last subscription.

        Args:
            callback: The relay's callback.
        """
        with self._subscriptions_lock:
            for index, (subscribed_callback, _loop) in enumerate(self.subscriptions):
                if subscribed_callback == callback:
                    del self.subscriptions[index]
                    break
            stopping = self._stop_future
            if not self.subscriptions and self.listen_task is not None and stopping is None:
                listening_loop = self.listen_task.get_loop()
                if not listening_loop.is_running():
                    listening_loop = asyncio.get_running_loop()
                # A concurrent Future lets subscribers on any event loop await the same release.
                stopping = asyncio.run_coroutine_threadsafe(self._stop_listening(self.listen_task), listening_loop)
                self._stop_future = stopping
                stopping.add_done_callback(self._forget_stopped_listener)
        if stopping is not None:
            await ResourceCompletion.wait(asyncio.wrap_future(stopping))

    def _forget_stopped_listener(self, stopped: Future[None]) -> None:
        """Publish an empty listener slot only after its transport has finished closing."""
        with self._subscriptions_lock:
            if self._stop_future is stopped:
                self.listen_task = None
                self._stop_future = None

    async def _stop_listening(self, listen_task: asyncio.Task[None]) -> None:
        """Stops the listener and closes its transport on the listener's event loop."""
        listen_task.cancel()
        async with OutboxRelease(lambda: self.close):
            with contextlib.suppress(asyncio.CancelledError):
                await listen_task

    def dispatch(self, topics: frozenset[str]) -> None:
        """Gives a received signal to every subscribed relay - on the event loop of its subscription, also
        for a signal of another thread (a write of a thread with an event loop of its own).

        Args:
            topics: The topics it names.
        """
        try:
            running_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        with self._subscriptions_lock:
            subscriptions = tuple(self.subscriptions)
        for callback, loop in subscriptions:
            if loop is running_loop:
                self.call_callback(callback, topics)
                continue
            try:
                loop.call_soon_threadsafe(self.call_callback, callback, topics)
            except RuntimeError:
                # The relay's event loop is closed - the relay is gone.
                logger.warning("%s: a relay's event loop is closed, its wakeup is dropped", type(self).__name__)

    def call_callback(self, callback: WakeupCallback, topics: frozenset[str]) -> None:
        """Calls a relay's callback, an error logged - the relay polls anyway.

        Args:
            callback: The callback.
            topics: The topics of the signal.
        """
        try:
            callback(topics)
        except Exception:
            logger.exception("%s: a relay's wakeup callback raised", type(self).__name__)

    async def listen(self, connection: str | DatabaseClient) -> None:
        """Listens for signals, giving each to ``dispatch()``, until cancelled - raises when the
        subscription is lost, to be retried; returns once it gives up for good.

        Args:
            connection: The relay's selected client, or a connection alias for standalone use.
        """
        raise NotImplementedError

    async def close(self) -> None:
        """Releases what sending and listening opened - nothing by default."""

    async def keep_listening(self, listen: Callable[[], Awaitable[None]]) -> None:
        """Runs ``listen`` again after it fails, with backoff, until it fails
        ``WAKEUP_MAX_RECONNECT_ATTEMPTS`` times in a row.

        Args:
            listen: The listening.
        """
        failures = 0
        loop = asyncio.get_running_loop()
        while True:
            started_at = loop.time()
            try:
                await listen()
                # Given up on its own - the relays keep polling.
                return
            except Exception:
                # A subscription that ran a while before failing starts the count again.
                failures = 1 if loop.time() - started_at >= WAKEUP_HEALTHY_SECONDS else failures + 1
                if failures >= WAKEUP_MAX_RECONNECT_ATTEMPTS:
                    logger.exception(
                        "%s: listening for outbox signals failed %d times - the relays keep polling",
                        type(self).__name__,
                        failures,
                    )
                    return
                logger.warning("%s: listening for outbox signals failed, retrying", type(self).__name__)
            await asyncio.sleep(WAKEUP_RECONNECT_BACKOFF_SECONDS * 2 ** max(failures - 1, 0))
