from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.postgresql.fields.constants import (
    POSTGRESQL_INET_TYPE,
    POSTGRESQL_LQUERY_TYPE,
    POSTGRESQL_LTREE_TYPE,
    POSTGRESQL_LTXTQUERY_TYPE,
)
from hare.dialects.postgresql.fields.ranges.range import Range
from hare.exceptions import ValidationError
from hare.fields import Field
from hare.sql.functions.cast import Cast
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.postgresql.fields.ltree_field import LtreeField
    from hare.dialects.postgresql.fields.multiranges.multi_range_field import MultiRangeField
    from hare.dialects.postgresql.fields.ranges.range_field import RangeField
    from hare.models import Model


class PostgresqlValueEncoders:
    """The encoders of array and range lookups - each called as ``(value, model, field,
    dialect)``, like ``ValueEncoders``."""

    @staticmethod
    def encode_range(value: Any, obj: Model, field: Field[Any], dialect: Dialect) -> Term:
        """Encodes the value of a range-against-range lookup - a ``Range`` or a 2-tuple, converted as
        an equality filter's value and wrapped as a term.
        """
        return ValueWrapper(dialect.types.get_db_value(cast("RangeField", field), value, obj))

    @staticmethod
    def encode_network(value: Any, obj: Model, field: Field[Any], dialect: Dialect) -> Term:
        """Encodes the value of a subnet lookup - an address or a network, as text or an ``ipaddress``
        object - cast to ``inet``, which takes both.

        Raises:
            ValidationError: ``value`` is neither an address nor a network.
        """
        try:
            interface = ipaddress.ip_interface(str(value))
        except ValueError as error:
            raise ValidationError(
                f"{field.model_field_name}: {value!r} is neither an address nor a network"
            ) from error
        return Cast(ValueWrapper(str(interface)), POSTGRESQL_INET_TYPE)

    @staticmethod
    def encode_ltree(value: Any, obj: Model, field: Field[Any], dialect: Dialect) -> Term:
        """Encodes the path of a tree lookup (``__ancestor_of``/``__descendant_of``) - a ``str`` or a
        list of labels - cast to ``ltree``.

        Raises:
            ValidationError: ``value`` isn't an ltree path.
        """
        return Cast(ValueWrapper(cast("LtreeField", field).get_path_text(value)), POSTGRESQL_LTREE_TYPE)

    @staticmethod
    def get_query_text(value: Any, field: Field[Any], query_type: str) -> str:
        """The text of an ``lquery``/``ltxtquery`` value.

        Args:
            value: The query.
            field: The filtered field, named in an error message.
            query_type: The query's type, named in an error message.

        Returns:
            The text.

        Raises:
            ValidationError: ``value`` isn't a ``str``.
        """
        if not isinstance(value, str):
            raise ValidationError(
                f"{field.model_field_name}: expected an {query_type} str, got {field.get_value_for_message(value)}"
            )
        return value

    @staticmethod
    def encode_lquery(value: Any, obj: Model, field: Field[Any], dialect: Dialect) -> Term:
        """Encodes the pattern of ``__matches`` - a ``str`` cast to ``lquery``."""
        query_text = PostgresqlValueEncoders.get_query_text(value, field, POSTGRESQL_LQUERY_TYPE)
        return Cast(ValueWrapper(query_text), POSTGRESQL_LQUERY_TYPE)

    @staticmethod
    def encode_lquery_list(value: Any, obj: Model, field: Field[Any], dialect: Dialect) -> Term:
        """Encodes the patterns of ``__matches_any`` - a list of ``str``, an array of ``lquery``.

        Raises:
            ValidationError: ``value`` isn't a non-empty list or tuple of ``str``.
        """
        if not isinstance(value, (list, tuple)) or not value:
            raise ValidationError(
                f"{field.model_field_name}: expected a non-empty list of lquery str, "
                f"got {field.get_value_for_message(value)}"
            )
        query_texts = [PostgresqlValueEncoders.get_query_text(query, field, POSTGRESQL_LQUERY_TYPE) for query in value]
        return Cast(ValueWrapper(query_texts), f"{POSTGRESQL_LQUERY_TYPE}[]")

    @staticmethod
    def encode_ltxtquery(value: Any, obj: Model, field: Field[Any], dialect: Dialect) -> Term:
        """Encodes the query of ``__matches_text`` - a ``str`` cast to ``ltxtquery``."""
        query_text = PostgresqlValueEncoders.get_query_text(value, field, POSTGRESQL_LTXTQUERY_TYPE)
        return Cast(ValueWrapper(query_text), POSTGRESQL_LTXTQUERY_TYPE)

    @staticmethod
    def encode_range_or_element(value: Any, obj: Model, field: Field[Any], dialect: Dialect) -> Term:
        """Encodes the value of a range's ``__contains``: a ``Range``/2-tuple, or a bare value - cast
        to the range's element type, since an uncast parameter would pick the range-contains-range
        form of ``@>``.
        """
        range_field = cast("RangeField", field)
        if isinstance(value, (Range, tuple)):
            return ValueWrapper(dialect.types.get_db_value(range_field, value, obj))
        return Cast(ValueWrapper(range_field.coerce_bound(value)), range_field.ELEMENT_SQL_TYPE)

    @staticmethod
    def encode_multi_range(value: Any, obj: Model, field: Field[Any], dialect: Dialect) -> Term:
        """Encodes the value of a multirange lookup - a list of ranges, or one ``Range``/2-tuple taken
        as a multirange of that range alone - cast to the column's multirange type.
        """
        multi_range_field = cast("MultiRangeField", field)
        if multi_range_field.is_range_value(value):
            value = [value]
        db_value = dialect.types.get_db_value(multi_range_field, value, obj)
        return Cast(ValueWrapper(db_value), multi_range_field.get_column_type(dialect))

    @staticmethod
    def encode_multi_range_or_element(value: Any, obj: Model, field: Field[Any], dialect: Dialect) -> Term:
        """Encodes the value of a multirange's ``__contains``: ranges as ``encode_multi_range`` takes
        them, or a bare value - cast to the ranges' element type.
        """
        multi_range_field = cast("MultiRangeField", field)
        if multi_range_field.is_range_value(value) or isinstance(value, (list, tuple, set)):
            return PostgresqlValueEncoders.encode_multi_range(value, obj, field, dialect)
        range_field = multi_range_field.range_field
        return Cast(ValueWrapper(range_field.coerce_bound(value)), range_field.ELEMENT_SQL_TYPE)
