from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.contrib.notify import NotificationListener
from hare.contrib.outbox.wakeups.constants import (
    DEFAULT_WAKEUP_CHANNEL,
    WAKEUP_MAX_RECONNECT_ATTEMPTS,
    WAKEUP_RECONNECT_BACKOFF_SECONDS,
)
from hare.contrib.outbox.wakeups.outbox_wakeup import OutboxWakeup
from hare.exceptions import ConfigurationError
from hare.instrumentation.capture.change_capturing import ChangeCapturing

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient


class ListenNotifyWakeup(OutboxWakeup):
    """Wakes the relays through the database's notification channels - a notification per topic in the
    transaction of the events, which the database delivers once it commits, joining the same
    notifications of one transaction. A database without listen/notify (``Features.supports_listen_notify``)
    is refused.

    Args:
        channel: The channel.
    """

    def __init__(self, channel: str = DEFAULT_WAKEUP_CHANNEL) -> None:
        super().__init__()
        self.channel = channel
        #: The listener of the relays' subscription.
        self.listener: NotificationListener | None = None

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
            transaction, savepoint_span, topics_by_savepoint = self._get_topic_scope(connection)
            unsent_topics.difference_update(*topics_by_savepoint.values())
            if not unsent_topics:
                return
            notified = self._register_topic_bucket(
                connection, transaction, savepoint_span, topics_by_savepoint, signal_on_commit=False
            )
        notifying_connection: Any = connection
        for topic in sorted(unsent_topics):
            await notifying_connection.notify(self.channel, topic)
        if notified is not None:
            notified.update(unsent_topics)

    async def listen(self, connection: str | DatabaseClient) -> None:
        self.listener = NotificationListener(
            connection,
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
