from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool
from starlette.middleware.errors import ServerErrorMiddleware
from starlette.middleware.exceptions import ExceptionMiddleware
from starlette.routing import Mount

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

    @classmethod
    def wrap(cls, handler: Callable[..., Any]) -> TransactionFailingHandler:
        return handler if isinstance(handler, cls) else cls(handler)

    @classmethod
    def prepare_mounts(cls, application: FastAPI) -> None:
        """Register the rollback hook where mounted FastAPI apps handle their exceptions."""
        seen = {id(application)}
        applications = [application]
        while applications:
            for route in applications.pop().routes:
                if not isinstance(route, Mount) or not isinstance(child := route.app, FastAPI) or id(child) in seen:
                    continue
                seen.add(id(child))
                cls.install(child)
                applications.append(child)

    @classmethod
    def install(cls, application: FastAPI) -> None:
        application.exception_handlers = {
            key: cls.wrap(handler) for key, handler in application.exception_handlers.items()
        }
        middleware = application.middleware_stack
        while middleware is not None:
            if isinstance(middleware, ServerErrorMiddleware) and middleware.handler is not None:
                middleware.handler = cls.wrap(middleware.handler)
            if isinstance(middleware, ExceptionMiddleware):
                for key, handler in application.exception_handlers.items():
                    if key not in (500, Exception):
                        middleware.add_exception_handler(key, handler)
                break
            middleware = getattr(middleware, "app", None)

    async def __call__(self, connection: HTTPConnection, error: Exception) -> Any:
        RequestTransaction.fail_request(connection.scope, error)
        if inspect.iscoroutinefunction(self.handler) or inspect.iscoroutinefunction(type(self.handler).__call__):
            return await self.handler(connection, error)
        return await run_in_threadpool(self.handler, connection, error)
