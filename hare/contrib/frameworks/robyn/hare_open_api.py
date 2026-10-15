"""The OpenAPI schema of a Hare Robyn application: the parameters of the request queries its
handlers take."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from robyn.openapi import OpenAPI

from hare.contrib.frameworks.constants import PATH_PARAMETER_SOURCE, QUERY_PARAMETER_SOURCE
from hare.contrib.frameworks.parameter_annotation import ParameterAnnotation
from hare.contrib.frameworks.robyn.constants import (
    COMPONENT_REFERENCE_TEMPLATE,
    HARE_ROUTE_HANDLER_ATTRIBUTE,
    OPENAPI_PATH_PARAMETER_PATTERN,
    OPENAPI_PATH_PARAMETER_REPLACEMENT,
)

if TYPE_CHECKING:  # pragma: nocoverage
    from collections.abc import Callable

    from robyn.openapi import RouteOpenAPIMeta

    from hare.contrib.request_query.request_query import RequestQuery


class HareOpenAPI(OpenAPI):
    """Robyn's OpenAPI schema with the parameters of each request query a handler takes: a query
    parameter each - a list one repeated - with its type, limits, enum and description, a path
    parameter for one marked ``InPath()``. ``HareRobyn`` uses it unless given another."""

    def add_openapi_path_obj(
        self,
        route_type: str,
        endpoint: str,
        openapi_name: str,
        openapi_tags: list[str],
        handler: Callable[..., Any],
        auth_required: bool = False,
        meta: RouteOpenAPIMeta | None = None,
    ) -> None:
        super().add_openapi_path_obj(
            route_type, endpoint, openapi_name, openapi_tags, handler, auth_required=auth_required, meta=meta
        )
        route_handler = getattr(handler, HARE_ROUTE_HANDLER_ATTRIBUTE, None)
        if route_handler is None or not route_handler.request_queries:
            return
        path = re.sub(OPENAPI_PATH_PARAMETER_PATTERN, OPENAPI_PATH_PARAMETER_REPLACEMENT, endpoint)
        operation = self.openapi_spec["paths"].get(path, {}).get(route_type)
        if operation is None:
            return
        parameters: list[dict[str, Any]] = operation.setdefault("parameters", [])
        for request_query_class in route_handler.request_queries.values():
            for parameter in self.get_parameters(request_query_class):
                parameters[:] = [
                    existing
                    for existing in parameters
                    if (existing["name"], existing["in"]) != (parameter["name"], parameter["in"])
                ]
                parameters.append(parameter)

    def get_parameters(self, request_query_class: type[RequestQuery[Any]]) -> list[dict[str, Any]]:
        """The OpenAPI parameters of a request query - the schemas they name go to the schema's
        components.

        Args:
            request_query_class: The request query class.

        Returns:
            One parameter object per parameter of the class.
        """
        request_query_class.prepare_parameters()
        schema = request_query_class.model_json_schema(ref_template=COMPONENT_REFERENCE_TEMPLATE)
        self.openapi_spec["components"].setdefault("schemas", {}).update(schema.pop("$defs", {}))
        required = set(schema.get("required", []))
        parameters = []
        for name, field_info in request_query_class.model_fields.items():
            property_schema = dict(schema["properties"][name])
            description = property_schema.pop("description", None)
            in_path = ParameterAnnotation.is_in_path(field_info)
            parameter: dict[str, Any] = {
                "name": name,
                "in": PATH_PARAMETER_SOURCE if in_path else QUERY_PARAMETER_SOURCE,
                "required": in_path or name in required,
                "schema": property_schema,
            }
            if description is not None:
                parameter["description"] = description
            parameters.append(parameter)
        return parameters
