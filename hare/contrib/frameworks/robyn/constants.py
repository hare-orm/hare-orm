from __future__ import annotations

from http import HTTPStatus

#: The content type of a JSON answer.
JSON_CONTENT_TYPE = "application/json"

#: The status of request parameters a request query refused, as Robyn answers its own invalid
#: parameters - by number: its name is UNPROCESSABLE_ENTITY before Python 3.13 and
#: UNPROCESSABLE_CONTENT from it.
INVALID_PARAMETERS_STATUS = HTTPStatus(422)

#: What Robyn's own validation errors say an invalid request is.
VALIDATION_ERROR_TITLE = "Validation Error"

#: The names Robyn passes the request under to a handler parameter without an annotation.
REQUEST_ARGUMENT_NAMES = frozenset({"r", "req", "request"})

#: The parameter a route's function takes the request as when its handler takes none - named so
#: no handler parameter collides with it.
ROUTE_REQUEST_ARGUMENT = "hare_request"

#: The attribute of a route's function holding its ``RouteHandler`` - Robyn copies it onto the
#: function it wraps the route's function in, where ``HareOpenAPI`` reads it.
HARE_ROUTE_HANDLER_ATTRIBUTE = "hare_route_handler"

#: A path parameter in a Robyn route (``/books/:pk``) and its OpenAPI form (``/books/{pk}``).
OPENAPI_PATH_PARAMETER_PATTERN = r":(\w+)"
OPENAPI_PATH_PARAMETER_REPLACEMENT = r"{\1}"

#: Where a request query's JSON schema names the schemas of its parameters' types.
COMPONENT_REFERENCE_TEMPLATE = "#/components/schemas/{model}"
