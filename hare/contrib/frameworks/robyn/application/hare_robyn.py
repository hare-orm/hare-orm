from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from robyn import Headers, Request, Response, Robyn, SubRouter

from hare.contrib.frameworks.hare_lifecycle import HareLifecycle
from hare.contrib.frameworks.health_routes import HealthRoutes
from hare.contrib.frameworks.request_state import RequestState
from hare.contrib.frameworks.request_transaction import RequestTransaction
from hare.contrib.frameworks.robyn.constants import JSON_CONTENT_TYPE
from hare.contrib.frameworks.robyn.hare_exception_handlers import HareExceptionHandlers
from hare.contrib.frameworks.robyn.hare_open_api import HareOpenAPI
from hare.contrib.frameworks.robyn.route_handler import RouteHandler

if TYPE_CHECKING:  # pragma: nocoverage
    from robyn import HttpMethod
    from robyn.openapi import OpenAPI

    from hare.core.config import HareConfig
from hare.contrib.frameworks.robyn.application.hare_sub_router import HareSubRouter


class HareRobyn(Robyn):
    """A Robyn application with Hare::

        app = HareRobyn(__file__, hare_config=HARE_CONFIG, atomic_requests=True)

        @app.get("/books", response_model=PageSchema[BookSchema])
        async def list_books(books: BookQuery) -> Page[Book]:
            return await books.page()

    - binds the models of the configuration when it is created, opens a Hare context when a
      worker process starts - before the application's own ``startup_handler`` - and closes it
      when the process stops;
    - checks every request query once the models are set up, so a wrong one fails at startup;
    - builds each request query a handler takes (``books: BookQuery``) from the request's query
      string and path parameters, and documents their parameters in the OpenAPI schema
      (``HareOpenAPI``);
    - validates what a handler returns with its route's ``response_model`` - a row, a list, a page
      (``PageSchema[BookSchema]``) - read from its attributes;
    - answers ``InvalidRequestQuery`` with 422, ``DoesNotExist`` with 404, ``IntegrityError`` with
      409 and ``RequestQueryForbidden`` with 403 (``HareExceptionHandlers``);
    - starts each request with its own state (``RequestState``): an empty count of repeated
      queries, no writes of other requests;
    - with ``atomic_requests``, runs each handler in a transaction - a handler marked
      ``RequestTransaction.skip`` runs without it.

    A router of the application is a ``HareSubRouter``. Robyn keeps one exception handler per
    application and hands it to each route when the route is declared: an application answering
    errors itself sets its handler with ``exception()`` before declaring routes and calls
    ``HareExceptionHandlers.get_response()`` first in it.

    Args:
        file_object: What ``Robyn()`` takes first - the application's module file.
        hare_config: The Hare configuration, as for ``Hare.init(config=...)``.
        atomic_requests: True for a transaction per request on the default connection, the
            connection names for one on each of them, False for none.
        openapi: The OpenAPI schema, a ``HareOpenAPI`` by default.
        health_routes: The readiness and liveness routes to add - none when None.
        kwargs: What ``Robyn()`` takes besides.

    Raises:
        ConfigurationError: ``atomic_requests`` is neither a bool nor a sequence of names, or the
            configuration is wrong.
    """

    def __init__(
        self,
        file_object: str,
        *,
        hare_config: Mapping[str, Any] | HareConfig | str,
        atomic_requests: bool | Sequence[str] = False,
        openapi: OpenAPI | None = None,
        health_routes: HealthRoutes | None = None,
        **kwargs: Any,
    ) -> None:
        self.hare = HareLifecycle(hare_config, atomic_requests=atomic_requests)
        self.hare.bind_models()
        self.own_startup_handler: Callable[[], Any] | None = None
        self.own_shutdown_handler: Callable[[], Any] | None = None
        super().__init__(file_object, openapi=openapi or HareOpenAPI(), **kwargs)
        self.exception(HareExceptionHandlers.handle)
        super().startup_handler(self.start_hare)
        super().shutdown_handler(self.stop_hare)
        self.before_request()(self.reset_request_state)
        if health_routes is not None:
            self.add_health_routes(health_routes)

    def add_health_routes(self, health_routes: HealthRoutes) -> None:
        """Adds the readiness and liveness routes - outside the request's transaction and the OpenAPI
        schema.

        Args:
            health_routes: The routes.
        """

        @RequestTransaction.skip
        async def readiness() -> Response:
            status_code, body = await health_routes.get_readiness_answer()
            return HareRobyn.get_json_response(status_code, body)

        @RequestTransaction.skip
        async def liveness() -> Response:
            status_code, body = health_routes.get_liveness_answer()
            return HareRobyn.get_json_response(status_code, body)

        self.get(health_routes.readiness_path, include_in_schema=False)(readiness)
        self.get(health_routes.liveness_path, include_in_schema=False)(liveness)

    @staticmethod
    def get_json_response(status_code: int, body: dict[str, Any]) -> Response:
        """A JSON response.

        Args:
            status_code: The status.
            body: The body.

        Returns:
            The response.
        """
        return Response(
            status_code=status_code,
            headers=Headers({"Content-Type": JSON_CONTENT_TYPE}),
            description=json.dumps(body),
        )

    def add_route(
        self, route_type: HttpMethod | str, endpoint: str, handler: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        route_handler = RouteHandler(handler, kwargs.get("response_model"), kwargs.get("status_code"))
        route_handler.transaction_connection_names = self.hare.transaction_connection_names
        return super().add_route(route_type, endpoint, route_handler.build_function(), *args, **kwargs)

    def include_router(self, router: SubRouter) -> None:
        super().include_router(router)
        if isinstance(router, HareSubRouter):
            for route_handler in router.route_handlers:
                route_handler.transaction_connection_names = self.hare.transaction_connection_names

    def startup_handler(self, handler: Callable[[], Any]) -> None:
        self.own_startup_handler = handler

    def shutdown_handler(self, handler: Callable[[], Any]) -> None:
        self.own_shutdown_handler = handler

    async def start_hare(self) -> None:
        """Opens the Hare context, then runs the application's own startup handler.

        Raises:
            ConfigurationError: See ``HareLifecycle.start()``.
        """
        await self.hare.start()
        await self.run_own_handler(self.own_startup_handler)

    async def stop_hare(self) -> None:
        """Runs the application's own shutdown handler, then closes the Hare context."""
        try:
            await self.run_own_handler(self.own_shutdown_handler)
        finally:
            await self.hare.stop()

    @staticmethod
    async def run_own_handler(handler: Callable[[], Any] | None) -> None:
        """Runs an application's own startup or shutdown handler, sync or async.

        Args:
            handler: The handler, None for none.
        """
        if handler is None:
            return
        result = handler()
        if inspect.isawaitable(result):
            await result

    @staticmethod
    async def reset_request_state(request: Request) -> Request:
        """Starts a request with its own state (``RequestState``).

        Args:
            request: The request.

        Returns:
            The request, unchanged.
        """
        RequestState.reset()
        return request
