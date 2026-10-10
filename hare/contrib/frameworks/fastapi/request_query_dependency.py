"""Request queries as FastAPI dependencies: the query's parameters become the route's query and
path parameters, validated and documented by FastAPI, and the query gets the request."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Annotated, Any

from annotated_types import BaseMetadata
from fastapi import Depends, Path, Query, Request

from hare.contrib.frameworks.parameter_annotation import ParameterAnnotation
from hare.contrib.request_query.constants import REQUEST_ARGUMENT_NAME

if TYPE_CHECKING:  # pragma: nocoverage
    from pydantic.fields import FieldInfo

    from hare.contrib.request_query.request_query import RequestQuery


class RequestQueryDependency:
    """Builds a request query for a route::

        @app.get("/books", response_model=PageSchema[BookSchema])
        async def list_books(books: BookQuery = RequestQueryDependency.provide(BookQuery)) -> Page[Book]:
            return await books.page()

    Each parameter of the class is a query parameter of the route - a list one repeated
    (``?status__in=draft&status__in=published``), with the description, the limits and the enum
    of its type in the OpenAPI schema - or a path parameter, marked ``InPath()`` in the
    class (``pk: Annotated[KeyColumns[int, int], InPath()]``). A parameter read from one text value
    (``KeyColumns``, ``CommaSeparated``) reaches the query as that text. FastAPI reads the
    parameters when a route is declared: the models must be bound before (``HareFastAPI``, or
    ``Hare.bind_models()``) for ``Meta.filters`` and the descriptions.

    Args:
        request_query_class: The request query class.
    """

    def __init__(self, request_query_class: type[RequestQuery[Any]]) -> None:
        self.request_query_class = request_query_class

    @classmethod
    def provide(cls, request_query_class: type[RequestQuery[Any]]) -> Any:
        """The dependency building a request query from the request.

        Args:
            request_query_class: The request query class.

        Returns:
            ``Depends()`` of the class's dependency.
        """
        return Depends(cls(request_query_class))

    @property
    def __signature__(self) -> inspect.Signature:
        """The parameters FastAPI reads the query from - the class's, and the request."""
        self.request_query_class.prepare_parameters()
        parameters = [inspect.Parameter(REQUEST_ARGUMENT_NAME, inspect.Parameter.KEYWORD_ONLY, annotation=Request)]
        for name, field_info in self.request_query_class.model_fields.items():
            parameters.append(self.get_parameter(name, field_info))
        return inspect.Signature(parameters, return_annotation=self.request_query_class)

    @staticmethod
    def get_parameter(name: str, field_info: FieldInfo) -> inspect.Parameter:
        """One parameter of the query as FastAPI reads it.

        Args:
            name: The parameter.
            field_info: Its pydantic field.

        Returns:
            The parameter: its type - ``str`` for a type read from one text value - with its
            limits and a ``Query()`` describing it, a ``Path()`` for one marked ``InPath()``.
        """
        constraints = [item for item in field_info.metadata if isinstance(item, BaseMetadata)]
        in_path = ParameterAnnotation.is_in_path(field_info)
        marker = Path(description=field_info.description) if in_path else Query(description=field_info.description)
        annotation: Any = Annotated[(ParameterAnnotation.get_type(field_info), *constraints, marker)]
        default = (
            inspect.Parameter.empty
            if field_info.is_required() or in_path
            else field_info.get_default(call_default_factory=True, validated_data={})
        )
        return inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=annotation, default=default)

    async def __call__(self, request: Request, **values: Any) -> RequestQuery[Any]:
        return self.request_query_class(request=request, **values)
