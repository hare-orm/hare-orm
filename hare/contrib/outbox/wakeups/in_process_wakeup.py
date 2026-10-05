from __future__ import annotations

import asyncio

from hare.contrib.outbox.wakeups.outbox_wakeup import OutboxWakeup


class InProcessWakeup(OutboxWakeup):
    """Wakes the relays of the same process right after the transaction of the events commits - no
    broker. Relays of other processes keep polling."""

    async def signal(self, topics: frozenset[str]) -> None:
        self.dispatch(topics)

    async def listen(self, connection_alias: str) -> None:
        # The signals come from this process - nothing to listen to.
        await asyncio.Event().wait()
