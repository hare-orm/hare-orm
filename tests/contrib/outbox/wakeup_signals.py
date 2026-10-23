"""Waiting for a wakeup's subscription by its signals: a subscription connects on its own time and a
signal sent before it is lost - a signal is sent again until it arrives, never after a guessed delay."""

from collections.abc import Callable

from hare.contrib.outbox.wakeups.outbox_wakeup import OutboxWakeup
from tests.contrib.outbox.models import DemoOutboxEvent
from tests.utils.waiting import wait_until

#: The topic of the signals sent only to find out the subscription works - left out of what a test checks.
PROBE_TOPIC = "hare.test.probe"


async def send_signal(wakeup: OutboxWakeup, topic: str) -> None:
    """Sends a signal of a topic as events written outside a transaction send it.

    Args:
        wakeup: The wakeup the signal goes through.
        topic: The topic of the signal.
    """
    await wakeup.signal_written(DemoOutboxEvent.get_connection(for_write=True), (topic,))


async def signal_until_arrived(wakeup: OutboxWakeup, topic: str, has_arrived: Callable[[], object]) -> None:
    """Sends a signal of a topic again and again until it arrived.

    Args:
        wakeup: The wakeup the signals go through.
        topic: The topic of the signals.
        has_arrived: Whether the signal arrived - a relay delivered, a callback got it.
    """

    async def arrived() -> bool:
        if not has_arrived():
            await send_signal(wakeup, topic)
        return bool(has_arrived())

    await wait_until(arrived)
