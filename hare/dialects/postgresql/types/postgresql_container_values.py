from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from hare.exceptions import ValidationError

if TYPE_CHECKING:
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.fields.data.containers.array_field import ArrayField
    from hare.fields.field import Field


class PostgresqlContainerValues:
    """How PostgreSQL stores an array - ``<element type>[]``, an element that is an array a dimension
    more, each element written and read through the PostgreSQL types of its field; an element can
    always be NULL."""

    def __init__(self, types: TypeRegistry) -> None:
        """
        Args:
            types: The PostgreSQL types.
        """
        self.types = types

    def get_column_type(self, field: Field[Any]) -> str | None:
        """``<element type>[]`` - None when the element field has no column type on PostgreSQL.

        Args:
            field: The array field.

        Returns:
            The column type.
        """
        # Local import: the dialect constants import the types module.
        from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT

        base_field = field.base_field  # type: ignore[attr-defined]
        if not base_field.exists_on(POSTGRESQL_DIALECT):
            return None
        return f"{base_field.get_column_type(POSTGRESQL_DIALECT)}[]"

    def prepare_db(self, field: Field[Any]) -> Callable[[Any, Any], Any]:
        """Prepare child conversions; rectangular shape remains a per-value database rule."""
        array_field: ArrayField = field  # type: ignore[assignment]
        write = array_field.get_dialect_db_writer(self.types)
        nested = isinstance(array_field.base_field, type(array_field))

        def write_value(value: Any, instance: Any) -> Any:
            db_value = write(value, instance)
            if isinstance(db_value, list) and nested:
                try:
                    self.get_shape(db_value)
                except ValueError:
                    raise ValidationError(
                        f"{array_field.get_path()}: a PostgreSQL array of arrays holds sub-arrays of one length at "
                        f"each depth, got {array_field.get_value_for_message(value)}"
                    ) from None
            return db_value

        return write_value

    @classmethod
    def get_shape(cls, value: list[Any]) -> tuple[int, ...]:
        """The lengths of an array at each depth.

        Args:
            value: The array.

        Returns:
            The lengths.

        Raises:
            ValueError: Its sub-arrays aren't of one shape, or a value sits beside a sub-array.
        """
        if not any(isinstance(element, list) for element in value):
            return (len(value),)
        shapes = set()
        for element in value:
            if not isinstance(element, list):
                raise ValueError(element)
            shapes.add(cls.get_shape(element))
        if len(shapes) != 1:
            raise ValueError(shapes)
        return (len(value), *shapes.pop())

    def prepare_python(self, field: Field[Any]) -> Callable[[Any], Any]:
        """Prepare the array's sole read traversal."""
        return field.get_dialect_python_reader(self.types)  # type: ignore[attr-defined]
