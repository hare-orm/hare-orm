from __future__ import annotations

from typing import TYPE_CHECKING

from hare.instrumentation.declarations import ConnectionFailed
from hare.instrumentation.observers.observers import Observers

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient


class ConnectFailureReports:
    """A failure to open a connection to the database - opening a pool or a pool growing - counted
    and reported to the observers the same way by every driver."""

    @staticmethod
    def record(
        client: DatabaseClient,
        error: BaseException,
        *,
        attempt: int = 1,
        will_retry: bool = False,
        counted: bool = False,
    ) -> None:
        """Counts a failure to open a connection and reports it (``ConnectionFailed``).

        Args:
            client: The client.
            error: The driver's exception.
            attempt: The attempt of opening the pool, from 1.
            will_retry: Whether another attempt follows.
            counted: Whether the driver counted the failure itself (the Rust one, in its pool).
        """
        if not counted:
            client.pool_statistics.add_connect_failure()
        if Observers.is_observed(ConnectionFailed):
            Observers.notify(
                ConnectionFailed(
                    client.connection_alias,
                    client.pool_role,
                    client.get_address(),
                    type(error).__name__,
                    attempt,
                    will_retry,
                )
            )
