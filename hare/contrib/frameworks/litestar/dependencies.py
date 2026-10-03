"""Request queries as Litestar dependencies: the query's parameters become the handler's query,
path and header parameters, and the query gets the request."""

from __future__ import annotations

import dataclasses
import inspect
from typing import TYPE_CHECKING, Annotated, Any

from litestar import Request
from litestar.di import Provide
from litestar.params import ParameterKwarg, PathParameter, QueryParameter
from litestar.plugins import DIPlugin

from hare.contrib.frameworks.parameters import ParameterAnnotation
from hare.contrib.request_query.base import RequestQuery
from hare.contrib.request_query.constants import REQUEST_ARGUMENT_NAME

if TYPE_CHECKING:  # pragma: nocoverage
    from pydantic.fields import FieldInfo


class RequestQueryDIPlugin(DIPlugin):
    """Lets Litestar build a ``RequestQuery`` subclass as a dependency: each of its parameters is a
    parameter of the handler (a query parameter unless it is marked ``InPath()`` or its annotation
    says ``FromPath``/``FromHeader``/...), validated and documented in the OpenAPI schema, and the query receives the
    request. A parameter read from one text value (``KeyColumns``, ``CommaSeparated``) reaches
    the query as that text::

        @get("/books", dependencies={"books": RequestQueryDIPlugin.provide(BookQuery)})
        async def list_books(books: NamedDependency[BookQuery]) -> Page[Book]:
            return await books.page()

    ``HarePlugin`` is one. Without it, list ``RequestQueryDIPlugin()`` in ``plugins`` - an
    application's plugins come before the ones Litestar adds, whose pydantic DI plugin would
    otherwise take a request query as a plain pydantic model.
    """

    def has_typed_init(self, type_: Any) -> bool:
        return isinstance(type_, type) and issubclass(type_, RequestQuery)

    def get_typed_init(self, type_: Any) -> tuple[inspect.Signature, dict[str, Any]]:
        type_.prepare_parameters()
        annotations: dict[str, Any] = {}
        parameters = []
        for name, field_info in type_.model_fields.items():
            annotation = self.get_parameter_annotation(field_info)
            annotations[name] = annotation
            default = (
                inspect.Parameter.empty
                if field_info.is_required()
                else field_info.get_default(call_default_factory=True)
            )
            parameters.append(
                inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=annotation, default=default)
            )
        parameters.append(inspect.Parameter(REQUEST_ARGUMENT_NAME, inspect.Parameter.KEYWORD_ONLY, annotation=Request))
        annotations[REQUEST_ARGUMENT_NAME] = Request
        return inspect.Signature(parameters), annotations

    @classmethod
    def get_parameter_annotation(cls, field_info: FieldInfo) -> Any:
        """The annotation Litestar reads a parameter with.

        Args:
            field_info: The parameter's pydantic field.

        Returns:
            The field's annotation with its metadata - a query parameter unless the metadata makes
            it a path (``InPath()`` or Litestar's own), header or cookie one, described by the
            field's description unless the metadata describes it; ``str`` in place of a type read
            from one text value.
        """
        annotation = ParameterAnnotation.get_type(field_info)
        metadata = [
            dataclasses.replace(item, description=field_info.description)
            if isinstance(item, ParameterKwarg) and item.description is None
            else item
            for item in field_info.metadata
        ]
        if not any(isinstance(item, ParameterKwarg) for item in metadata):
            parameter_type = PathParameter if ParameterAnnotation.is_in_path(field_info) else QueryParameter
            metadata.append(parameter_type(description=field_info.description))
        return Annotated[(annotation, *metadata)]

    @staticmethod
    def provide(request_query_class: type[RequestQuery[Any]]) -> Provide:
        """The dependency building a request query from the request.

        Args:
            request_query_class: The request query class.

        Returns:
            The ``Provide`` of the class.
        """
        return Provide(request_query_class, sync_to_thread=False)
