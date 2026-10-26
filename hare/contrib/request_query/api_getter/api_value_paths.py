from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any

from hare.contrib.request_query.api_getter.constants import API_PATH_SEPARATOR, JSON_SCALAR_TYPES
from hare.exceptions import NoValuesFetched
from hare.query.queryset import QuerySet, RelatedQuerySet
from hare.query.queryset.single_rows.none_awaitable_type import NoneAwaitableType


class ApiValuePaths:
    """Paths of names joined by dots through objects and answers - each part an attribute or a key, a list
    on the way walked item by item."""

    @staticmethod
    def get_child(container: Any, name: str) -> Any:
        """The value of a name in a container - its key of a mapping, its attribute otherwise; a relation of
        a row the rows loaded with it (``select_related()``, ``prefetch_related()``), a list for a relation to
        many.

        Args:
            container: The container.
            name: The name.

        Returns:
            The value, None when the container has none of the name - a text, a number, a relation not
            loaded.
        """
        if isinstance(container, Mapping):
            return container.get(name)
        if isinstance(container, JSON_SCALAR_TYPES):
            return None
        try:
            value = getattr(container, name, None)
        except NoValuesFetched:
            return None
        if isinstance(value, RelatedQuerySet):
            return list(value.related_objects) if value._fetched else None
        # A relation not loaded, or one of an empty key - nothing on the other side.
        if isinstance(value, QuerySet | NoneAwaitableType):
            return None
        return value

    @staticmethod
    def set_child(container: Any, name: str, value: Any) -> None:
        """Sets the value of a name in a container - its key of a mapping, its attribute otherwise.

        Args:
            container: The container.
            name: The name.
            value: The value.
        """
        if isinstance(container, MutableMapping):
            container[name] = value
        else:
            setattr(container, name, value)

    @staticmethod
    def get_value_at_path(obj: Any, path: str | None) -> Any:
        """The value at a path, lists not walked.

        Args:
            obj: Where the path starts.
            path: The path, None for ``obj`` itself.

        Returns:
            The value, None when a part is missing.
        """
        if path is None:
            return obj
        for name in path.split(API_PATH_SEPARATOR):
            obj = ApiValuePaths.get_child(obj, name)
            if obj is None:
                return None
        return obj

    @staticmethod
    def get_containers(obj: Any, path: str) -> list[Any]:
        """The containers holding the last part of a path - lists on the way walked.

        Args:
            obj: Where the path starts.
            path: The path.

        Returns:
            The containers.
        """
        *parents, _name = path.split(API_PATH_SEPARATOR)
        containers = [obj]
        for parent in parents:
            next_containers = []
            for container in containers:
                value = ApiValuePaths.get_child(container, parent)
                if value is not None:
                    next_containers.extend(ApiValuePaths.to_list(value))
            containers = next_containers
        return containers

    @staticmethod
    def get_items(answer: Any, items_path: str | None) -> list[Any]:
        """The items of an answer - at ``items_path``, a single one made a list.

        Args:
            answer: The answer's JSON.
            items_path: The path of the items, None for the answer itself.

        Returns:
            The items.
        """
        items = ApiValuePaths.get_value_at_path(answer, items_path)
        if items is None:
            return []
        return ApiValuePaths.to_list(items)

    @staticmethod
    def to_list(value: Any) -> list[Any]:
        """A list as it is, any other value as a list of it.

        Args:
            value: The value.
        """
        return value if isinstance(value, list) else [value]
