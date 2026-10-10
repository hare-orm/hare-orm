from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.exceptions import QueryError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.query.queryset.queryset import QuerySet


class SchemaObjectCalls:
    """The schema objects a model declares that a queryset reaches - a materialized view to refresh, a
    sequence to read the next value of - through the schema editor of the connection."""

    @staticmethod
    def get_declared_schema_object(
        queryset: QuerySet[Any, Any], declared_objects: Sequence[Any], name: str, noun: str
    ) -> Any:
        """An object the model declares beside its table, by name.

        Args:
            queryset: The queryset.
            declared_objects: The objects of that sort the model declares.
            name: The object's name.
            noun: The sort of object, for the error message.

        Raises:
            QueryError: The model declares none of that name.
        """
        for declared_object in declared_objects:
            if declared_object.name == name:
                return declared_object
        raise QueryError(f"{queryset.model.__name__} declares no {noun} named {name!r}")

    @staticmethod
    def get_schema_editor(queryset: QuerySet[Any, Any]) -> BaseSchemaEditor:
        """A schema editor of the connection the model writes to.

        Args:
            queryset: The queryset.
        """
        connection = queryset.get_connection(for_write=True)
        return connection.dialect.schema_editor_class(connection)
