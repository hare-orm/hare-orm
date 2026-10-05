from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

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
        """
        return cast("ArrayField", field).get_dialect_db_value(value, instance, self.types)

    def to_python(self, field: Field[Any], value: Any) -> Any:
        """The Python value of an array the driver returned.

        Args:
            field: The array field.
            value: The value.

        Returns:
            The list of element values.
        """
        return cast("ArrayField", field).get_dialect_python_value(value, self.types)
