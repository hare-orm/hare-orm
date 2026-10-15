from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import ValidationError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.fields.data.containers.array_field import ArrayField
    from hare.fields.field import Field
    from hare.models import Model


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

        base_field = cast("ArrayField", field).base_field
        if not base_field.exists_on(POSTGRESQL_DIALECT):
            return None
        return f"{base_field.get_column_type(POSTGRESQL_DIALECT)}[]"

    def to_db(self, field: Field[Any], value: Any, instance: type[Model] | Model | None) -> Any:
        """The array as the driver binds it.

        Args:
            field: The array field.
            value: The Python value.
            instance: The model (class) it is written or compared for.

        Returns:
            The list of element values.

        Raises:
            ValidationError: An array of arrays holds sub-arrays of other lengths at one depth, or a
                value beside a sub-array - PostgreSQL's arrays are rectangular.
        """
        array_field = cast("ArrayField", field)
        db_value = array_field.get_dialect_db_value(value, instance, self.types)
        if isinstance(db_value, list) and isinstance(array_field.base_field, type(array_field)):
            try:
                self.get_shape(db_value)
            except ValueError:
                raise ValidationError(
                    f"{array_field.get_path()}: a PostgreSQL array of arrays holds sub-arrays of one length at "
                    f"each depth, got {array_field.get_value_for_message(value)}"
                ) from None
        return db_value

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

    def to_python(self, field: Field[Any], value: Any) -> Any:
        """The Python value of an array the driver returned.

        Args:
            field: The array field.
            value: The value.

        Returns:
            The list of element values.
        """
        return cast("ArrayField", field).get_dialect_python_value(value, self.types)
