"""``HareFastAPI`` - a FastAPI application with Hare."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from hare.contrib.frameworks.fastapi.hare_exception_handlers import HareExceptionHandlers
from hare.contrib.frameworks.fastapi.middleware.request_state_reset_middleware import RequestStateResetMiddleware
from hare.contrib.frameworks.fastapi.middleware.transaction_failing_handler import TransactionFailingHandler
from hare.contrib.frameworks.fastapi.middleware.transaction_middleware import TransactionMiddleware
from hare.contrib.frameworks.hare_lifecycle import HareLifecycle
from hare.contrib.frameworks.health_routes import HealthRoutes
from hare.contrib.frameworks.request_transaction import RequestTransaction

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.core.config import HareConfig

#: What ``FastAPI(lifespan=...)`` takes: the application's own lifespan.
type Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[Any]]


class HareFastAPI(FastAPI):
    """A FastAPI application with Hare::

        app = HareFastAPI(hare_config=HARE_CONFIG, atomic_requests=True, title="Books")

    - binds the models of the configuration when it is created - FastAPI reads a route's
      parameters when the route is declared, so the request queries' parameters
      (``Meta.filters``, the descriptions) need them; a router declared in a module imported
      before the application needs ``Hare.bind_models()`` called before it;
    - opens a Hare context when the application starts - before its own ``lifespan``, which already
      has the database - and closes it when it stops; the context is visible to every request;
    - checks every request query once the models are set up, so a wrong one fails at startup;
    - answers ``DoesNotExist`` with 404, ``IntegrityError`` with 409, ``InvalidRequestQuery`` with
      422 and ``RequestQueryForbidden`` with 403 - in FastAPI's own error format - unless the
      application answers them itself (``exception_handlers=``);
    - starts each request with an empty count of repeated queries (``RepeatedQueryDetector``);
    - with ``atomic_requests``, runs each HTTP request in a transaction (``TransactionMiddleware``)
      - a route whose endpoint is marked ``RequestTransaction.skip`` runs without it; a handler
      that raises rolls it back, even when an exception handler answers it.

    Args:
        hare_config: The Hare configuration, as for ``Hare.init(config=...)``.
        atomic_requests: True for a transaction per request on the default connection, the
            connection names for one on each of them, False for none.
        lifespan: The application's own lifespan - it runs inside the Hare context.
        health_routes: The readiness and liveness routes to add - none when None.
        kwargs: What ``FastAPI()`` takes.

    Raises:
        ConfigurationError: ``atomic_requests`` is neither a bool nor a sequence of names, or the
            configuration is wrong.
    """

    def __init__(
        self,
        *,
        hare_config: Mapping[str, Any] | HareConfig | str,
        atomic_requests: bool | Sequence[str] = False,
        lifespan: Lifespan | None = None,
        health_routes: HealthRoutes | None = None,
        **kwargs: Any,
    ) -> None:
        self.hare = HareLifecycle(hare_config, atomic_requests=atomic_requests)
        self.hare.bind_models()
        self.own_lifespan = lifespan
        super().__init__(lifespan=self.run_lifespan, **kwargs)
        for exception_type, handler in HareExceptionHandlers.get_handlers().items():
            self.exception_handlers.setdefault(exception_type, handler)
        if self.hare.transaction_connection_names:
            self.add_middleware(
                TransactionMiddleware,
                connection_aliases=self.hare.transaction_connection_names,
            )
        self.add_middleware(RequestStateResetMiddleware)
        if health_routes is not None:
            self.add_health_routes(health_routes)

    def add_health_routes(self, health_routes: HealthRoutes) -> None:
        """Adds the readiness and liveness routes - outside the request's transaction and the OpenAPI
        schema.

        Args:
            health_routes: The routes.
        """

        @RequestTransaction.skip
        async def readiness() -> JSONResponse:
            status_code, body = await health_routes.get_readiness_answer()
            return JSONResponse(body, status_code=status_code)

        @RequestTransaction.skip
        async def liveness() -> JSONResponse:
            status_code, body = health_routes.get_liveness_answer()
            return JSONResponse(body, status_code=status_code)

        self.add_api_route(health_routes.readiness_path, readiness, methods=["GET"], include_in_schema=False)
        self.add_api_route(health_routes.liveness_path, liveness, methods=["GET"], include_in_schema=False)

    def build_middleware_stack(self) -> Any:
        """Builds the application, each exception handler rolling back the request's transaction
        first when ``atomic_requests`` is on - the handlers the application added after it was
        created included.

        Returns:
            The application wrapped in its middleware.
        """
        if not self.hare.transaction_connection_names:
            return super().build_middleware_stack()
        exception_handlers = self.exception_handlers
        self.exception_handlers = {
            key: TransactionFailingHandler(handler) for key, handler in exception_handlers.items()
        }
        try:
            return super().build_middleware_stack()
        finally:
            self.exception_handlers = exception_handlers

    @asynccontextmanager
    async def run_lifespan(self, app: FastAPI) -> AsyncGenerator[Any]:
        """The Hare context around the application's own lifespan.

        Args:
            app: The application.

        Yields:
            The state the application's own lifespan yields, None without one.
        """
        async with self.hare.running():
            if self.own_lifespan is None:
                yield None
                return
            async with self.own_lifespan(app) as state:
                yield state
