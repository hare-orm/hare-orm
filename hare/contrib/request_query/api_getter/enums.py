from __future__ import annotations

from enum import StrEnum


class ApiValueMappingMode(StrEnum):
    """Which value a target several mappings set keeps."""

    #: The value of the last mapping, empty or not.
    LAST = "last"
    #: The first value that isn't empty.
    NOT_EMPTY = "not_empty"


class ApiRequestMode(StrEnum):
    """How a source is asked for the values of its key."""

    #: One request per value, the value in the URL (``".../users/{user_id}"``).
    PATH = "path"
    #: One request per value, the value as a query parameter.
    QUERY_ONE = "query_one"
    #: One request per batch of values, each one a repetition of the query parameter.
    QUERY_MANY = "query_many"


class ApiGetterTrigger(StrEnum):
    """What the objects whose values are loaded come from."""

    #: The rows a request reads - ``fetch()``, ``page()``, ``get()``.
    READ = "read"
    #: The rows a handler created.
    CREATE = "create"
    #: The rows a handler updated.
    UPDATE = "update"
