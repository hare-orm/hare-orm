import asyncio
from typing import Any


class AsyncEvents:
    """Wait for a task's synchronization event without hiding an earlier task failure."""

    @staticmethod
    async def wait_for(event: asyncio.Event, operation: asyncio.Future[Any]) -> None:
        waiter = asyncio.create_task(event.wait())
        try:
            await asyncio.wait((operation, waiter), return_when=asyncio.FIRST_COMPLETED)
            if not event.is_set():
                await operation
                raise AssertionError("The operation finished without setting its synchronization event")
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
