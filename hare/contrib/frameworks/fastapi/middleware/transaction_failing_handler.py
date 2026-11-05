from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from starlette.concurrency import run_in_threadpool

from hare.contrib.frameworks.request_transaction import RequestTransaction

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import HTTPConnection


class TransactionFailingHandler:
    """Mark the request transaction for rollback before calling the application's exception handler.

    Rollback occurs when the response starts, including a handled ``404`` or ``HTTPException(403)``.

    Args:
        handler: The application's exception handler.
    """

    def __init__(self, handler: Callable[..., Any]) -> None:
        self.handler = handler

    async def __call__(self, connection: HTTPConnection, error: Exception) -> Any:
        RequestTransaction.fail_request(connection.scope, error)
        if inspect.iscoroutinefunction(self.handler) or inspect.iscoroutinefunction(type(self.handler).__call__):
            return await self.handler(connection, error)
        return await run_in_threadpool(self.handler, connection, error)
