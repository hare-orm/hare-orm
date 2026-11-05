from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping
from functools import wraps
from typing import TYPE_CHECKING, Any

from robyn import FunctionInfo, Request, Response, Robyn, SubRouter

from hare.contrib.frameworks.hare_lifecycle import HareLifecycle
from hare.contrib.frameworks.health_routes import HealthRoutes
from hare.contrib.frameworks.request_state import RequestState
from hare.contrib.frameworks.request_transaction import RequestTransaction
from hare.contrib.frameworks.robyn.hare_exception_handlers import HareExceptionHandlers
from hare.contrib.frameworks.robyn.hare_open_api import HareOpenAPI
from hare.contrib.frameworks.robyn.route_handler import RouteHandler
from hare.core.connections.messages import CONNECTION_RELEASE_FAILED
from hare.core.constants import INITIALIZATION_RELEASE_FAILED
from hare.instrumentation.queries.query_tags import QueryTags
from hare.models.tenancy.tenancy import Tenancy

if TYPE_CHECKING:
    from robyn import HttpMethod
    from robyn.openapi import OpenAPI
    from robyn.router import GlobalMiddleware, Route, RouteMiddleware

    from hare.core.config import HareConfig
    from hare.core.hare_context import HareContext


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
      409, ``RequestQueryForbidden`` with 403 and ``ApiGetterError`` with 502 (``HareExceptionHandlers``);
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
        hare_context: An initialized context to borrow without closing it, None to create one.
        atomic_requests: True for a transaction per request on the automatically selected connection, the
            connection aliases for one on each of them, False for none.
        openapi: The OpenAPI schema, a ``HareOpenAPI`` by default.
        health_routes: The readiness and liveness routes to add - none when None.
        kwargs: What ``Robyn()`` takes besides.

    Raises:
        ConfigurationError: ``atomic_requests`` is neither a bool nor an iterable of names, or the
            configuration is wrong.
    """

    def __init__(
        self,
        file_object: str,
        *,
        hare_config: Mapping[str, Any] | HareConfig | str,
        hare_context: HareContext | None = None,
        atomic_requests: bool | Iterable[str] = False,
        openapi: OpenAPI | None = None,
        health_routes: HealthRoutes | None = None,
        **kwargs: Any,
    ) -> None:
        self.hare = HareLifecycle(hare_config, atomic_requests=atomic_requests, context=hare_context)
        self.hare.bind_models()
        self.application_startup_handler: Callable[[], Any] | None = None
        self.application_shutdown_handler: Callable[[], Any] | None = None
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
            return HareExceptionHandlers.get_json_response(status_code, body)

        @RequestTransaction.skip
        async def liveness() -> Response:
            status_code, body = health_routes.get_liveness_answer()
            return HareExceptionHandlers.get_json_response(status_code, body)

        self.get(health_routes.readiness_path, include_in_schema=False)(readiness)
        self.get(health_routes.liveness_path, include_in_schema=False)(liveness)

    def add_route(
        self, route_type: HttpMethod | str, endpoint: str, handler: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        middleware_start = len(self.middleware_router.route_middlewares)
        if RouteHandler.is_const_route(args, kwargs):
            result = super().add_route(route_type, endpoint, handler, *args, **kwargs)
        else:
            route_handler = RouteHandler(handler, kwargs.get("response_model"), kwargs.get("status_code"))
            result = super().add_route(route_type, endpoint, route_handler.build_function(), *args, **kwargs)
            self.router.routes[-1] = self.bind_entry(self.router.routes[-1])
        # Native authentication middleware is registered outside before_request().
        middlewares = self.middleware_router.route_middlewares
        for index in range(middleware_start, len(middlewares)):
            middlewares[index] = self.bind_entry(middlewares[index])
        return result

    def include_router(self, router: SubRouter) -> None:
        collections: tuple[list[Route] | list[GlobalMiddleware] | list[RouteMiddleware], ...] = (
            self.router.routes,
            self.middleware_router.global_middlewares,
            self.middleware_router.route_middlewares,
        )
        starts = tuple(len(collection) for collection in collections)
        previous_websockets = dict(self.web_socket_router.routes)
        super().include_router(router)
        # Robyn shares callback objects when including a router. Bind only the entries
        # copied into this application, so another application can reuse the router.
        for collection, start in zip(collections, starts, strict=True):
            for index in range(start, len(collection)):
                entry = collection[index]
                if getattr(entry, "is_const", False):
                    continue
                collection[index] = self.bind_entry(entry)
        for endpoint, handlers in self.web_socket_router.routes.items():
            if previous_websockets.get(endpoint) is not handlers:
                self.web_socket_router.routes[endpoint] = {
                    name: self.bind_function(function) for name, function in handlers.items()
                }

    def add_web_socket(self, endpoint: str, handlers: dict[str, FunctionInfo]) -> None:
        super().add_web_socket(endpoint, {name: self.bind_function(function) for name, function in handlers.items()})

    def bind_entry(self, entry: Any) -> Any:
        """Copy a native route/middleware entry with an application-scoped callback.

        Wrapping the native callback includes argument parsing and exception handlers,
        which execute outside the user route function. Shared router entries stay intact.
        """
        return entry._replace(function=self.bind_function(entry.function))

    def bind_function(self, function: FunctionInfo) -> FunctionInfo:
        """Preserve native callback arguments while binding execution to this application."""
        return FunctionInfo(
            self.scope_handler(function.handler, is_async=function.is_async),
            function.is_async,
            function.number_of_params,
            function.args,
            function.kwargs,
        )

    def before_request(self, endpoint: str | None = None) -> Callable[..., None]:
        register = super().before_request(endpoint)
        return lambda handler: register(self.scope_handler(handler, is_async=inspect.iscoroutinefunction(handler)))

    def after_request(self, endpoint: str | None = None) -> Callable[..., None]:
        register = super().after_request(endpoint)
        return lambda handler: register(self.scope_handler(handler, is_async=inspect.iscoroutinefunction(handler)))

    def scope_handler(self, handler: Callable[..., Any], *, is_async: bool) -> Callable[..., Any]:
        """Bind a callback to the application while preserving Robyn's sync or async execution path.

        A synchronous request hook must remain synchronous so its request-state changes stay in
        the context Robyn passes to subsequent callbacks. The application scope itself owns no task.
        """

        if not is_async:

            @wraps(handler)
            def scoped_sync(*args: Any, **kwargs: Any) -> Any:
                with self.hare.scope():
                    return handler(*args, **kwargs)

            return scoped_sync

        @wraps(handler)
        async def scoped(*args: Any, **kwargs: Any) -> Any:
            with self.hare.scope():
                result = handler(*args, **kwargs)
                return await result if inspect.isawaitable(result) else result

        return scoped

    def startup_handler(self, handler: Callable[[], Any]) -> None:
        self.application_startup_handler = handler

    def shutdown_handler(self, handler: Callable[[], Any]) -> None:
        self.application_shutdown_handler = handler

    async def start_hare(self) -> None:
        """Opens the Hare context, then runs the application's own startup handler.

        Raises:
            ConfigurationError: See ``HareLifecycle.start()``.
            BaseExceptionGroup: Startup and context cleanup both fail.
        """
        await self.hare.start()
        try:
            with self.hare.scope():
                await self.run_application_handler(self.application_startup_handler)
        except BaseException as error:
            try:
                await self.hare.stop()
            except BaseException as release_error:
                raise BaseExceptionGroup(INITIALIZATION_RELEASE_FAILED, [error, release_error]) from None
            raise

    async def stop_hare(self) -> None:
        """Runs the application's shutdown handler and closes its context, preserving both failures."""
        if self.hare.context is None:
            return
        try:
            with self.hare.scope():
                await self.run_application_handler(self.application_shutdown_handler)
        except BaseException as error:
            try:
                await self.hare.stop()
            except BaseException as release_error:
                raise BaseExceptionGroup(CONNECTION_RELEASE_FAILED, [error, release_error]) from None
            raise
        else:
            await self.hare.stop()

    @staticmethod
    async def run_application_handler(handler: Callable[[], Any] | None) -> None:
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
    def reset_request_state(request: Request) -> Request:
        """Starts a request with its own state (``RequestState``), no tenant scope and no query
        tags: Robyn runs the ``before_request`` hooks of every request in one context, so a scope a
        hook set (``Tenancy.set()``) would stay for the next request - one without a tenant would
        read the previous request's rows.

        Args:
            request: The request.

        Returns:
            The request, unchanged.
        """
        RequestState.reset()
        Tenancy.set(None)
        QueryTags.current.set(None)
        return request
