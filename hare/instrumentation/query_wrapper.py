from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from hare.instrumentation.query_call import QueryCall


class QueryWrapper:
    """Runs around every query-executing call while installed with ``Observers.wrap_queries()`` -
    a tracing span, a timer, a guard. Wrappers nest in order of installation, the first outermost;
    a wrapper runs in the task that issued the query, around the driver call itself.
    """

    async def around(self, call: QueryCall, proceed: Callable[[], Awaitable[Any]]) -> Any:
        """Runs around one call.

        Args:
            call: The call.
            proceed: Runs the call - the next wrapper's ``around()``, or the driver call.

        Returns:
            What ``proceed()`` returned.
        """
        return await proceed()

    def around_stream(self, call: QueryCall, proceed: Callable[[], AsyncIterator[Any]]) -> AsyncIterator[Any]:
        """Runs around one stream - for as long as it is read.

        Args:
            call: The call.
            proceed: Opens the stream - the next wrapper's, or the driver's.

        Returns:
            The batches of rows, each a list.
        """
        return proceed()
