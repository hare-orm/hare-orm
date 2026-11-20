from __future__ import annotations

from typing import TYPE_CHECKING

from hare.exceptions import UnSupportedError
from hare.instrumentation.declarations import PoolStatus

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient


class ClientPoolStatus:
    """Builds public pool status from driver occupancy and shared connection statistics."""

    @staticmethod
    def get_pool_status(client: DatabaseClient) -> PoolStatus | None:
        """What the client's pool of connections holds and has done right now.

        Returns:
            The status - of no connection while the pool isn't open but failed to open; None while
            it isn't open and opened none.

        Raises:
            UnSupportedError: The client reports no pool status (``Features.supports_pool_status``).
        """
        if not client.features.supports_pool_status:
            raise UnSupportedError(
                f"The {client.driver_name} client of {client.connection_alias!r} reports no pool status"
            )
        acquire_count, acquire_timeouts, acquire_wait_seconds_total, connect_count, connect_failures = (
            client.pool_statistics.get_counts()
        )
        occupancy = client.get_pool_occupancy()
        if occupancy is None:
            if not connect_failures:
                return None
            # A pool failing to open - its failures are what there is to see.
            occupancy = (0, 0, 0, *client.get_pool_bounds())
        size, idle, waiting, min_size, max_size = occupancy
        return PoolStatus(
            client.connection_alias,
            client.pool_role,
            client.tenant_schema,
            size,
            idle,
            size - idle,
            waiting,
            min_size,
            max_size,
            acquire_count,
            acquire_timeouts,
            acquire_wait_seconds_total,
            connect_count,
            connect_failures,
        )
