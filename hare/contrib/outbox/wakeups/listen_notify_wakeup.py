from __future__ import annotations

from collections.abc import Iterable
from functools import partial
from typing import TYPE_CHECKING, Any, cast
from weakref import WeakKeyDictionary

from hare.contrib.notify import NotificationListener
from hare.contrib.outbox.wakeups.constants import (
    DEFAULT_WAKEUP_CHANNEL,
    WAKEUP_MAX_RECONNECT_ATTEMPTS,
    WAKEUP_RECONNECT_BACKOFF_SECONDS,
)
from hare.contrib.outbox.wakeups.outbox_wakeup import OutboxWakeup, SavepointTopics
from hare.dialects.base.client.transaction_lifecycle.transaction_callbacks import TransactionCallbacks
from hare.exceptions import ConfigurationError
from hare.instrumentation.capture.change_capturing import ChangeCapturing

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.client.transaction_client import TransactionClient
    from hare.dialects.base.transactions.savepoints.savepoint_span import SavepointSpan


class ListenNotifyWakeup(OutboxWakeup):
    """Wakes the relays through the database's ``LISTEN``/``NOTIFY`` - a ``NOTIFY`` per topic in the
    transaction of the events, which the database delivers once it commits, joining the same
    notifications of one transaction. A database without ``LISTEN``/``NOTIFY`` is refused.

    Args:
        channel: The channel.
    """

    def __init__(self, channel: str = DEFAULT_WAKEUP_CHANNEL) -> None:
        super().__init__()
        self.channel = channel
        #: The listener of the relays' subscription.
        self.listener: NotificationListener | None = None
        #: The topics each open transaction sent a NOTIFY of, by the savepoint it ran in - one NOTIFY per
        #: topic; the database drops the NOTIFY of a savepoint rolled back, so its topics are sent again.
        self.notified_topics: WeakKeyDictionary[TransactionClient, SavepointTopics] = WeakKeyDictionary()

    def raise_if_unsupported(self, connection: DatabaseClient) -> None:
        if not connection.features.supports_listen_notify:
            raise ConfigurationError(
                f"ListenNotifyWakeup signals the relays on a notification channel - {connection.dialect.name} has "
                "none; leave the outbox without it, its relays poll"
            )

    async def signal_written(self, connection: DatabaseClient, topics: Iterable[str]) -> None:
        self.raise_if_unsupported(connection)
        unsent_topics = set(topics)
        notified: set[str] | None = None
        if ChangeCapturing.is_in_transaction(connection):
            connection_transaction = cast("TransactionClient", connection)
            transaction = connection_transaction._get_top_level_transaction()
            savepoint_span = TransactionCallbacks.get_registration_span(connection_transaction)
            notified_by_savepoint = self.notified_topics.setdefault(transaction, {})
            unsent_topics.difference_update(*notified_by_savepoint.values())
            if not unsent_topics:
                return
            notified = notified_by_savepoint.get(savepoint_span)
            if notified is None:
                notified = notified_by_savepoint[savepoint_span] = set()
                TransactionCallbacks.add_on_rollback_callback(
                    connection_transaction, partial(self.forget_notified, transaction, savepoint_span)
                )
        notifying_connection: Any = connection
        for topic in sorted(unsent_topics):
            await notifying_connection.notify(self.channel, topic)
        if notified is not None:
            notified.update(unsent_topics)

    def forget_notified(self, transaction: TransactionClient, savepoint_span: SavepointSpan | None) -> None:
        """Forgets the topics notified in a savepoint, or a transaction, that rolled back - the database
        dropped their NOTIFY.

        Args:
            transaction: The top-level transaction.
            savepoint_span: The savepoint, None for the transaction's own level.
        """
        notified_by_savepoint = self.notified_topics.get(transaction)
        if notified_by_savepoint is not None:
            notified_by_savepoint.pop(savepoint_span, None)

    async def signal(self, topics: frozenset[str]) -> None:
        # Every signal is a NOTIFY sent by signal_written() in the events' own transaction.
        raise NotImplementedError

    async def listen(self, connection_alias: str) -> None:
        self.listener = NotificationListener(
            connection_alias,
            self.channel,
            self.on_notification,
            reconnect_attempts=WAKEUP_MAX_RECONNECT_ATTEMPTS,
            backoff=WAKEUP_RECONNECT_BACKOFF_SECONDS,
            max_backoff_seconds=None,
        )
        await self.listener.run()

    def on_notification(self, payload: str) -> None:
        """Gives a notification to the relays.

        Args:
            payload: The topic.
        """
        self.dispatch(frozenset((payload,)) if payload else frozenset())

    async def close(self) -> None:
        if self.listener is not None:
            listener, self.listener = self.listener, None
            await listener.stop()
