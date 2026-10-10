"""A route handler of a Hare Robyn application: it builds the request queries the handler takes,
runs it in the request's transaction and validates its response with the route's response model."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Any
from urllib.parse import urlencode, urlunsplit

from pydantic import TypeAdapter
from robyn import Headers, Request, Response

from hare.contrib.frameworks.request_transaction import RequestTransaction
from hare.contrib.frameworks.robyn.constants import (
    HARE_ROUTE_HANDLER_ATTRIBUTE,
    JSON_CONTENT_TYPE,
    REQUEST_ARGUMENT_NAMES,
    ROUTE_REQUEST_ARGUMENT,
)
from hare.contrib.frameworks.robyn.hare_exception_handlers import HareExceptionHandlers
from hare.contrib.request_query.exceptions import InvalidRequestQuery
from hare.contrib.request_query.request_query import RequestQuery


class RouteHandler:
    """One route's handler and what Hare adds to it.

    A parameter of the handler annotated with a ``RequestQuery`` subclass gets the query built
    from the request's query string and path parameters - Robyn would read a pydantic model from
    the request's body. A ``response_model`` of the route validates what the handler returns - a
    row, a list, a page - from its attributes; Robyn itself only validates a returned dict.

    Args:
        handler: The route's handler.
        response_model: The route's response model, None without one.
        status_code: The route's success status, None for 200.
    """

    def __init__(
        self, handler: Callable[..., Any], response_model: Any = None, status_code: int | None = None
    ) -> None:
        self.handler = handler
        self.response_model = response_model
        self.status_code = status_code
        self.response_adapter: TypeAdapter[Any] | None = (
            None if response_model is None else TypeAdapter(response_model)
        )
        self.transaction_connection_names: tuple[str | None, ...] = ()
        self.signature = inspect.signature(handler)
        self.request_queries: dict[str, type[RequestQuery[Any]]] = {
            name: parameter.annotation
            for name, parameter in self.signature.parameters.items()
            if isinstance(parameter.annotation, type) and issubclass(parameter.annotation, RequestQuery)
        }
        self.request_argument = self.get_request_argument()

    def get_request_argument(self) -> str | None:
        """The handler's own parameter taking the request.

        Returns:
            Its name, None when the handler takes no request.
        """
        for name, parameter in self.signature.parameters.items():
            if parameter.annotation is Request or (
                name in REQUEST_ARGUMENT_NAMES and parameter.annotation is inspect.Parameter.empty
            ):
                return name
        return None

    def build_function(self) -> Callable[..., Awaitable[Any]]:
        """The function Robyn runs for the route - the handler's name, documentation and
        parameters, the request in place of each request query.

        Returns:
            The function.
        """

        async def route_function(**arguments: Any) -> Any:
            return await self.call(arguments)

        parameters = [
            parameter for name, parameter in self.signature.parameters.items() if name not in self.request_queries
        ]
        if self.request_argument is None:
            parameters.append(
                inspect.Parameter(ROUTE_REQUEST_ARGUMENT, inspect.Parameter.KEYWORD_ONLY, annotation=Request)
            )
        return_annotation = self.signature.return_annotation if self.response_model is None else self.response_model
        route_function.__signature__ = self.signature.replace(  # type: ignore[attr-defined]
            parameters=parameters, return_annotation=return_annotation
        )
        route_function.__name__ = getattr(self.handler, "__name__", route_function.__name__)
        route_function.__qualname__ = getattr(self.handler, "__qualname__", route_function.__qualname__)
        route_function.__doc__ = self.handler.__doc__
        route_function.__module__ = self.handler.__module__
        setattr(route_function, HARE_ROUTE_HANDLER_ATTRIBUTE, self)
        return route_function

    async def call(self, arguments: dict[str, Any]) -> Any:
        """Runs the handler for a request.

        Args:
            arguments: What Robyn passes the route's function.

        Returns:
            The handler's response - validated with the response model - or ``422`` for request
            parameters a request query refused.
        """
        request: Request = arguments[self.request_argument or ROUTE_REQUEST_ARGUMENT]
        handler_arguments = {name: value for name, value in arguments.items() if name != ROUTE_REQUEST_ARGUMENT}
        try:
            for name, request_query_class in self.request_queries.items():
                handler_arguments[name] = self.build_request_query(request_query_class, request)
            if not self.transaction_connection_names or RequestTransaction.is_skipped(self.handler):
                return await self.run(handler_arguments)
            return await RequestTransaction(self.transaction_connection_names).run(
                lambda: self.run(handler_arguments), self.get_status_code
            )
        except InvalidRequestQuery as error:
            return HareExceptionHandlers.get_invalid_request_query_response(request, error)

    async def run(self, handler_arguments: dict[str, Any]) -> Any:
        """Runs the handler and validates its response.

        Args:
            handler_arguments: The handler's arguments.

        Returns:
            The response - a JSON ``Response`` of the response model when the route has one.
        """
        result = self.handler(**handler_arguments)
        if inspect.isawaitable(result):
            result = await result
        if self.response_adapter is None or isinstance(result, Response):
            return result
        validated = self.response_adapter.validate_python(result, from_attributes=True)
        return Response(
            status_code=self.status_code or HTTPStatus.OK.value,
            headers=Headers({"Content-Type": JSON_CONTENT_TYPE}),
            description=self.response_adapter.dump_json(validated).decode(),
        )

    def get_status_code(self, response: Any) -> int:
        """The status of a handler's response.

        Args:
            response: The response.

        Returns:
            A ``Response``'s own status, else the route's success status.
        """
        if isinstance(response, Response):
            return int(response.status_code)
        return self.status_code or HTTPStatus.OK.value

    @staticmethod
    def build_request_query(request_query_class: type[RequestQuery[Any]], request: Request) -> RequestQuery[Any]:
        """A request query from a request: its query string - a repeated name for each of its
        values - and its path parameters.

        Args:
            request_query_class: The request query class.
            request: The request.

        Returns:
            The query, with the request's whole address for the links of a page.

        Raises:
            InvalidRequestQuery: See ``RequestQuery.__init__()``.
        """
        pairs = [(name, value) for name, values in request.query_params.to_dict().items() for value in values]
        request_query_class.prepare_parameters()
        path_values = {
            name: value
            for name, value in dict(request.path_params or {}).items()
            if name in request_query_class.model_fields
        }
        request_query = request_query_class.from_query_parameters(pairs, request=request, **path_values)
        url = request.url
        request_query.set_request_url(urlunsplit((url.scheme, url.host, url.path, urlencode(pairs), "")))
        return request_query
