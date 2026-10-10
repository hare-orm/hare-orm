from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.contrib.notify import NotificationListener
from hare.contrib.outbox.broker_arguments import BrokerArguments
from hare.contrib.outbox.wakeups.constants import (
    DEFAULT_WAKEUP_CHANNEL,
    WAKEUP_MAX_RECONNECT_ATTEMPTS,
    WAKEUP_RECONNECT_BACKOFF_SECONDS,
)
from hare.contrib.outbox.wakeups.outbox_wakeup import OutboxWakeup

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient


class ListenNotifyWakeup(OutboxWakeup):
    """Wakes the relays through the database's ``LISTEN``/``NOTIFY`` - a ``NOTIFY`` per topic in the
    transaction of the events, which the database delivers once it commits, joining the same
    notifications of one transaction. On a connection without ``LISTEN``/``NOTIFY`` the events are
    written without a signal and the relays poll.

    Args:
        channel: The channel.

    Raises:
        ConfigurationError: ``channel`` isn't a non-empty string.
    """

    def __init__(self, channel: str = DEFAULT_WAKEUP_CHANNEL) -> None:
        super().__init__()
        BrokerArguments.require_text("channel", channel)
        self.channel = channel
        #: The listener of the relays' subscription.
        self.listener: NotificationListener | None = None

    async def signal_written(self, connection: DatabaseClient, topics: Iterable[str]) -> None:
        if not connection.features.supports_listen_notify:
            return
        notifying_connection: Any = connection
        for topic in sorted(set(topics)):
            await notifying_connection.notify(self.channel, topic)

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
