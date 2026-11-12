from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, TypeVar

from hare.contrib.outbox.constants import OUTBOX_WAKEUP_META_OPTION
from hare.dialects.base.client.database_client import DatabaseClient

if TYPE_CHECKING:
    from hare.contrib.outbox.outbox_event import OutboxEvent
    from hare.contrib.outbox.wakeups.outbox_wakeup import OutboxWakeup


EventType = TypeVar("EventType", bound="OutboxEvent")


class OutboxWakeups:
    """Selects and schedules the notification configured for an outbox model."""

    @staticmethod
    def get_wakeup(model: type[EventType]) -> OutboxWakeup | None:
        """The wakeup the events of the model signal - ``Meta.outbox_wakeup``.

        Returns:
            The wakeup, None when the model declares none.
        """
        return getattr(model._meta.meta, OUTBOX_WAKEUP_META_OPTION, None)

    @staticmethod
    def raise_if_wakeup_unsupported(
        model: type[EventType], connection: DatabaseClient, wakeup: OutboxWakeup | None = None
    ) -> None:
        """Refuses writing events the wakeup can't signal on ``connection`` - before they are written, not
        after they committed.

        Args:
            connection: The connection the events are written on.
            wakeup: The wakeup, the model's ``Meta.outbox_wakeup`` when None.

        Raises:
            ConfigurationError: The wakeup can't signal on the connection's database.
        """
        wakeup = wakeup if wakeup is not None else model.get_wakeup()
        if wakeup is not None:
            wakeup.raise_if_unsupported(connection)

    @staticmethod
    async def signal_wakeup(
        model: type[EventType], connection: DatabaseClient, topics: Iterable[str], wakeup: OutboxWakeup | None = None
    ) -> None:
        """Signals the relays that events of ``topics`` were written - once ``connection``'s transaction
        commits, at once outside a transaction.

        Args:
            connection: The connection the events were written on.
            topics: Their topics.
            wakeup: The wakeup, the model's ``Meta.outbox_wakeup`` when None.
        """
        wakeup = wakeup if wakeup is not None else model.get_wakeup()
        if wakeup is not None:
            await wakeup.signal_written(connection, topics)
