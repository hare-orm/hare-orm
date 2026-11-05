from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from hare.contrib.outbox.constants import OUTBOX_RELEASE_FAILED
from hare.core.resource_completion import ResourceCompletion


class KafkaConnection:
    """Own a lazily started producer, or borrow one the application has already started.

    A failed start releases its producer before another attempt. Startup and close share
    one lock; a borrowed producer is neither started nor stopped by this owner.

    Args:
        create_producer: Creates a producer with the transport's delivery settings.
        producer: An already started application producer, or None to create one on use.
    """

    def __init__(self, create_producer: Callable[[], Any], producer: Any = None) -> None:
        self.create_producer = create_producer
        self.producer = producer
        self.closes_producer = producer is None
        self.producer_lock = asyncio.Lock()

    async def get_producer(self) -> Any:
        """Return the started producer, retaining startup and cleanup failures together."""
        async with self.producer_lock:
            if self.producer is None:
                producer = self.create_producer()
                try:
                    await producer.start()
                except BaseException as error:
                    try:
                        await ResourceCompletion.wait(producer.stop())
                    except BaseException as release_error:
                        if release_error is error:
                            raise
                        raise BaseExceptionGroup(OUTBOX_RELEASE_FAILED, [error, release_error]) from None
                    raise
                self.producer = producer
            return self.producer

    async def close(self) -> None:
        """Finish stopping an owned producer before allowing another start."""
        async with self.producer_lock:
            if self.closes_producer and self.producer is not None:
                producer, self.producer = self.producer, None
                await ResourceCompletion.wait(producer.stop())
