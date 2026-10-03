from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.postgresql.fields.ranges.range import Range
from hare.exceptions import ValidationError
from hare.fields import Field
from hare.sql.functions.cast import Cast
from hare.sql.terms.array import Array
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.postgresql.fields.array import ArrayField
    from hare.dialects.postgresql.fields.ranges.range_field import RangeField
    from hare.models import Model


class PostgresqlValueEncoders:
    """The encoders of array and range lookups - each called as ``(value, model, field,
    dialect)``, like ``ValueEncoders``."""

    @staticmethod
    def encode_range(value: Any, instance: Model, field: Field[Any], dialect: Dialect) -> Term:
        """Encodes the value of a range-against-range lookup - a ``Range`` or a 2-tuple, converted as
        an equality filter's value and wrapped as a term.
        """
        return ValueWrapper(dialect.types.get_db_value(cast("RangeField", field), value, instance))

    @staticmethod
    def encode_range_or_element(value: Any, instance: Model, field: Field[Any], dialect: Dialect) -> Term:
        """Encodes the value of a range's ``__contains``: a ``Range``/2-tuple, or a bare value - cast
        to the range's element type, since an uncast parameter would pick the range-contains-range
        form of ``@>``.
        """
        range_field = cast("RangeField", field)
        if isinstance(value, (Range, tuple)):
            return ValueWrapper(dialect.types.get_db_value(range_field, value, instance))
        return Cast(ValueWrapper(range_field.coerce_bound(value)), range_field.ELEMENT_SQL_TYPE)

    @staticmethod
    def encode_array(value: Any | Sequence[Any], instance: Model, field: Field[Any], dialect: Dialect) -> Any:
        """Encodes an array compared with an array column: each element goes through the array's
        ``base_field``, and the literal is cast to the column's type - PostgreSQL would otherwise
        infer the smallest type that fits.

        Raises:
            ValidationError: ``value`` isn't a list, tuple or set.
        """
        array_field = cast("ArrayField", field)
        if not isinstance(value, (list, tuple, set)):
            raise ValidationError(
                f"{field.model_field_name}: expected a list/tuple/set, got {field.get_value_for_message(value)}"
            )
        encoded_elements = [
            array_field.encode_element(index, element, instance) for index, element in enumerate(value)
        ]
        # The type from the field itself - an array annotation's field (ArrayAgg's result, an
        # item of a nested array) is bound to no model.
        return Cast(Array(*encoded_elements), array_field.get_column_type(dialect))

    @staticmethod
    def encode_array_item(
        value: tuple[int, Any], instance: Model, field: Field[Any], dialect: Dialect
    ) -> tuple[int, Any]:
        """Encodes an ``__item=(index, comparison_value)`` filter value - the comparison value goes
        through the array's ``base_field``, the index is checked and passed on unchanged.

        Raises:
            ValidationError: ``value`` isn't an ``(index, comparison_value)`` pair, or the index
                isn't an ``int`` within the range an array subscript can address.
        """
        # Local import: the array functions import the array field, which imports this module.
        from hare.dialects.postgresql.functions.array.array_subscript import ArraySubscript

        if not isinstance(value, (tuple, list)) or len(value) != 2:
            raise ValidationError(f"{field.model_field_name}__item expects an (index, value) pair")
        index, item_value = value
        base_field = cast("ArrayField", field).base_field
        return ArraySubscript.get_validated_index(index), dialect.types.get_db_value(base_field, item_value, instance)
