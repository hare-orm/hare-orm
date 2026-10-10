from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from hare.dialects.base.client.constants import PING_SQL
from hare.dialects.base.client.declarations import PingResult
from hare.exceptions import DatabaseError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient


class ConnectionPing:
    """Whether a connection is usable: ``SELECT 1``, bounded by a timeout of its own - a server that
    stopped answering without closing the socket never makes a driver raise. Only a database error
    (``DBConnectionError``, ``OperationalError``) and the timeout mean unusable; any other exception
    is a programming error and propagates."""

    @staticmethod
    async def run(client: DatabaseClient, timeout: float) -> PingResult:
        """Pings a connection.

        Args:
            client: The connection's client.
            timeout: Seconds to wait for the answer.

        Returns:
            How it went.
        """
        start_time = time.perf_counter()
        try:
            await asyncio.wait_for(client.execute(PING_SQL), timeout)
        except (DatabaseError, TimeoutError) as error:
            return PingResult(False, None, type(error).__name__)
        return PingResult(True, (time.perf_counter() - start_time) * 1000, None)
