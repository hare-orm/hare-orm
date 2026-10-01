from __future__ import annotations

#: The content type of a JSON answer.
JSON_CONTENT_TYPE = "application/json"

#: What Robyn's own validation errors say an invalid request is.
VALIDATION_ERROR_TITLE = "Validation Error"

#: Where the validation errors say a parameter comes from.
QUERY_PARAMETER_SOURCE = "query"
PATH_PARAMETER_SOURCE = "path"

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
COMPONENT_REF_TEMPLATE = "#/components/schemas/{model}"
