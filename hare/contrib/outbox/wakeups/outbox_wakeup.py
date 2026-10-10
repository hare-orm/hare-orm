from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterable
from functools import partial
from typing import TYPE_CHECKING, cast
from weakref import WeakKeyDictionary

from hare.contrib.outbox.wakeups.constants import (
    WAKEUP_HEALTHY_SECONDS,
    WAKEUP_MAX_RECONNECT_ATTEMPTS,
    WAKEUP_RECONNECT_BACKOFF_SECONDS,
)
from hare.core.log import logger
from hare.dialects.base.client.transaction_lifecycle.transaction_callbacks import TransactionCallbacks
from hare.instrumentation.capture.change_capturing import ChangeCapturing

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.client.transaction_client import TransactionClient

#: What a relay gets from a wakeup - the topics of the events written, empty when not known.
WakeupCallback = Callable[[frozenset[str]], None]


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

    def __init__(self) -> None:
        #: The topics of the events each open transaction wrote - signalled once it commits.
        self.pending_topics: WeakKeyDictionary[TransactionClient, set[str]] = WeakKeyDictionary()
        #: The relays' callbacks.
        self.callbacks: list[WakeupCallback] = []
        #: The task listening for signals while a relay is subscribed.
        self.listen_task: asyncio.Task[None] | None = None

    async def signal_written(self, connection: DatabaseClient, topics: Iterable[str]) -> None:
        """Signals events written on ``connection`` - once its transaction commits, at once outside one.

        Args:
            connection: The connection the events were written on.
            topics: Their topics.
        """
        if not ChangeCapturing.is_in_transaction(connection):
            await self.signal_safely(frozenset(topics))
            return
        transaction = cast("TransactionClient", connection)
        pending = self.pending_topics.get(transaction)
        if pending is None:
            pending = self.pending_topics[transaction] = set()
            TransactionCallbacks.add_on_commit_callback(transaction, partial(self.signal_pending, transaction))
        pending.update(topics)

    async def signal_pending(self, transaction: TransactionClient) -> None:
        """Signals the topics a committed transaction wrote.

        Args:
            transaction: The transaction.
        """
        topics = self.pending_topics.pop(transaction, None)
        if topics:
            await self.signal_safely(frozenset(topics))

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

    async def subscribe(self, callback: WakeupCallback, connection_alias: str) -> None:
        """Calls ``callback`` on every signal from now on - a relay's subscription.

        Args:
            callback: The relay's callback - called on the event loop's thread, never awaited.
            connection_alias: The connection of the relay's outbox model.
        """
        self.callbacks.append(callback)
        if self.listen_task is None:
            self.listen_task = asyncio.get_running_loop().create_task(
                self.keep_listening(partial(self.listen, connection_alias))
            )

    async def unsubscribe(self, callback: WakeupCallback) -> None:
        """Stops calling ``callback`` - the listening stops with the last subscription.

        Args:
            callback: The relay's callback.
        """
        with contextlib.suppress(ValueError):
            self.callbacks.remove(callback)
        if not self.callbacks and self.listen_task is not None:
            listen_task, self.listen_task = self.listen_task, None
            listen_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listen_task
            await self.close()

    def dispatch(self, topics: frozenset[str]) -> None:
        """Gives a received signal to every subscribed relay.

        Args:
            topics: The topics it names.
        """
        for callback in list(self.callbacks):
            try:
                callback(topics)
            except Exception:
                logger.exception("%s: a relay's wakeup callback raised", type(self).__name__)

    async def listen(self, connection_alias: str) -> None:
        """Listens for signals, giving each to ``dispatch()``, until cancelled - raises when the
        subscription is lost, to be retried; returns once it gives up for good.

        Args:
            connection_alias: The connection of the relays' outbox model.
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
            except asyncio.CancelledError:
                raise
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
