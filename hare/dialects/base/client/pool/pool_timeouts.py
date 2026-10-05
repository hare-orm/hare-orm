from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.client.constants import POOL_TIMEOUT_MESSAGE
from hare.exceptions import PoolTimeoutError
from hare.instrumentation.declarations import PoolAcquireTimedOut
from hare.instrumentation.observers.observers import Observers

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient


class PoolTimeouts:
    """A wait for a connection of a pool that ran out of ``pool_acquire_timeout`` - one exception and
    one text for every driver, counted and reported to the observers."""

    @staticmethod
    def get_error(client: DatabaseClient, waited_seconds: float, *, counted: bool = False) -> PoolTimeoutError:
        """The exception of a wait that ran out, counted and reported (``PoolAcquireTimedOut``).

        Args:
            client: The client whose pool ran out.
            waited_seconds: How long the call waited.
            counted: Whether the driver counted the timeout itself (the Rust one, in its pool).

        Returns:
            The exception for the caller to raise.
        """
        if not counted:
            client.pool_statistics.add_timeout()
        if Observers.is_observed(PoolAcquireTimedOut):
            occupancy = client.get_pool_occupancy()
            size, _, waiting, _, max_size = occupancy if occupancy is not None else (0, 0, 0, 0, 0)
            Observers.notify(
                PoolAcquireTimedOut(
                    client.connection_alias,
                    client.pool_role,
                    client.tenant_schema,
                    waited_seconds,
                    size,
                    max_size,
                    waiting,
                )
            )
        return PoolTimeoutError(
            POOL_TIMEOUT_MESSAGE.format(seconds=waited_seconds, connection_alias=client.connection_alias)
        )
