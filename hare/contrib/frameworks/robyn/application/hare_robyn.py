from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from robyn import Request, Robyn, SubRouter

from hare.contrib.frameworks.lifecycle import HareLifecycle
from hare.contrib.frameworks.robyn.exception_handlers import HareExceptionHandlers
from hare.contrib.frameworks.robyn.openapi import HareOpenAPI
from hare.contrib.frameworks.robyn.routes import RouteHandler
from hare.contrib.repeated_queries import RepeatedQueryDetector

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
    - starts each request with an empty count of repeated queries (``RepeatedQueryDetector``);
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
        kwargs: What ``Robyn()`` takes besides.

    Raises:
        ConfigurationError: ``atomic_requests`` is neither a bool nor a sequence of names, or the
            configuration is wrong.
    """

    def __init__(
        self,
        file_object: str,
        *,
        hare_config: dict[str, Any] | HareConfig,
        atomic_requests: bool | Sequence[str] = False,
        openapi: OpenAPI | None = None,
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
        self.before_request()(self.reset_repeated_queries)

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
    async def reset_repeated_queries(request: Request) -> Request:
        """Starts a request with an empty count of repeated queries.

        Args:
            request: The request.

        Returns:
            The request, unchanged.
        """
        RepeatedQueryDetector.reset_counts()
        return request
