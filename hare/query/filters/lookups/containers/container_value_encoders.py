from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import ValidationError
from hare.fields.data.containers.container_field import ContainerField
from hare.sql.terms.containers import ArrayElementTerm, ContainerLiteral

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.fields.data.containers.array_field import ArrayField
    from hare.fields.data.containers.map_field import MapField
    from hare.fields.field import Field
    from hare.models import Model


class ContainerValueEncoders:
    """The encoders of container lookups' values - each called as ``(value, model, field, dialect)``:
    a container value is written as the dialect writes the field's column, held values each through
    its own field."""

    @staticmethod
    def get_held_value_term(field: Field[Any], value: Any, obj: Model, dialect: Dialect) -> Any:
        """A held value compared with a value of a container - a container as a literal of its type,
        anything else as its field binds it.

        Args:
            field: The field of the held value.
            value: The value.
            obj: The model the filter is built for.
            dialect: The dialect the query runs on.

        Returns:
            The value or its term.
        """
        db_value = dialect.types.get_db_value(field, value, obj)
        if isinstance(field, ContainerField) and db_value is not None:
            return ContainerLiteral(db_value, field)
        return db_value

    @staticmethod
    def encode_container(value: Any, obj: Model, field: Field[Any], dialect: Dialect) -> ContainerLiteral:
        """Encodes a container compared with a container column - typed as the column.

        Raises:
            ValidationError: The value isn't of the container's shape, or a held value is refused.
        """
        db_value = dialect.types.get_db_value(field, value, obj)
        if db_value is None:
            raise ValidationError(f"{field.model_field_name}: expected a value to compare with, got None")
        return ContainerLiteral(db_value, field)

    @staticmethod
    def encode_array_item(value: Any, obj: Model, field: Field[Any], dialect: Dialect) -> tuple[int, Any]:
        """Encodes an ``__item=(index, value)`` filter value - the value through the array's
        ``base_field``, the index checked.

        Raises:
            ValidationError: ``value`` isn't an ``(index, value)`` pair, or the index isn't an int in
                range.
        """
        if not isinstance(value, (tuple, list)) or len(value) != 2:
            raise ValidationError(f"{field.model_field_name}__item expects an (index, value) pair")
        index, item_value = value
        base_field = cast("ArrayField", field).base_field
        return (
            ArrayElementTerm.get_validated_index(index),
            ContainerValueEncoders.get_held_value_term(base_field, item_value, obj, dialect),
        )

    @staticmethod
    def encode_map_key(value: Any, obj: Model, field: Field[Any], dialect: Dialect) -> Any:
        """Encodes a key a map is checked for, through the map's ``key_field``.

        Raises:
            ValidationError: The key field refuses the value.
        """
        if value is None:
            raise ValidationError(f"{field.model_field_name}: a map key is never None")
        return dialect.types.get_db_value(cast("MapField", field).key_field, value, obj)

    @staticmethod
    def encode_map_keys(value: Any, obj: Model, field: Field[Any], dialect: Dialect) -> list[Any]:
        """Encodes the keys a map is checked for, each through the map's ``key_field``.

        Raises:
            ValidationError: ``value`` isn't a list, tuple or set, or the key field refuses a key.
        """
        if not isinstance(value, (list, tuple, set)):
            raise ValidationError(f"{field.model_field_name}: expected a list/tuple/set of keys, got {value!r}")
        return [ContainerValueEncoders.encode_map_key(key, obj, field, dialect) for key in value]
