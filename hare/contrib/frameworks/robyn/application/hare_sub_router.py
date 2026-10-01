from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from robyn import SubRouter

from hare.contrib.frameworks.robyn.exception_handlers import HareExceptionHandlers
from hare.contrib.frameworks.robyn.routes import RouteHandler

if TYPE_CHECKING:  # pragma: nocoverage
    from robyn import HttpMethod


class HareSubRouter(SubRouter):
    """A Robyn ``SubRouter`` whose handlers take request queries, have their responses validated
    with the route's ``response_model`` and their errors answered as ``HareRobyn``'s - and run in
    the request's transaction once the application includes it::

        books = HareSubRouter(prefix="/books")


        @books.get("/", response_model=PageSchema[BookSchema])
        async def list_books(books: BookQuery) -> Page[Book]:
            return await books.page()


        app.include_router(books)
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        #: The handlers of the router's routes and of the routers it includes.
        self.route_handlers: list[RouteHandler] = []
        # Robyn hands a route the exception handler of the router declaring it.
        self.exception(HareExceptionHandlers.handle)

    def add_route(
        self, route_type: HttpMethod | str, endpoint: str, handler: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        route_handler = RouteHandler(handler, kwargs.get("response_model"), kwargs.get("status_code"))
        self.route_handlers.append(route_handler)
        return super().add_route(route_type, endpoint, route_handler.build_function(), *args, **kwargs)

    def include_router(self, router: SubRouter) -> None:
        super().include_router(router)
        if isinstance(router, HareSubRouter):
            self.route_handlers.extend(router.route_handlers)
