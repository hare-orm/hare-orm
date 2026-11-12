from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

from hare.classes.value_descriptions import ValueDescriptions
from hare.dialects.clickhouse.enums import ClickhouseTypeName
from hare.dialects.clickhouse.fields.declarations import (
    Int128Field,
    Int256Field,
    UInt64Field,
    UInt128Field,
    UInt256Field,
)
from hare.exceptions import ConfigurationError
from hare.fields.data.boolean_field import BooleanField
from hare.fields.data.network.ip_address_field import IPAddressField
from hare.fields.data.network.ipv4_address_field import IPv4AddressField
from hare.fields.data.numeric.big_int_field import BigIntField
from hare.fields.data.numeric.float_field import FloatField
from hare.fields.data.temporal.date_field import DateField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.text.text_field import TextField
from hare.fields.data.uuid_field import UUIDField
from hare.fields.field import Field

if TYPE_CHECKING:
    from hare.query.filters.lookups.field_lookup import FieldLookup
    from hare.sql.terms.term import Term


class DynamicField(Field[Any]):
    """``Dynamic`` - supported scalar and container values, each stored with its ClickHouse type::

        value = DynamicField(null=True)

    Accepts booleans, integers, floats, text, Decimal, dates, datetimes, UUIDs, IP addresses, and
    lists, tuples and dicts of supported values; enum members use their values. Integers select
    the first fitting Int64/UInt64/Int128/UInt128/Int256/UInt256 type. Unsupported objects or values
    outside the supported storage ranges raise ValidationError when written.

    A value is read back as it was written; a filter matches held values of the filter value's type
    only (``value=5`` doesn't match ``"5"``) - an int any held int, a decimal any held decimal, however
    wide - and a path named by a type reads the held values of that type, NULL for the others
    (``value__String__startswith="a"``, ``value__Int64__gt=3``). ClickHouse
    orders no rows by a ``Dynamic`` column - order by such a path. The column holds NULL itself;
    ``null=False`` refuses None on write.

    Args:
        max_types: The most types the column keeps in columns of their own - the rest share one
            (``Dynamic(max_types=N)``); ClickHouse's default when None.

    Raises:
        ConfigurationError: ``max_types`` isn't an int from 0 to 254.
    """

    field_type = object
    COLUMN_TYPE_FROM_DIALECT = True
    is_orderable = False
    keeps_native_db_values = False

    #: Each path segment -> the type of the held values it reads, and their field.
    PATH_FIELDS: ClassVar[dict[str, tuple[str, Field[Any]]]] = {
        ClickhouseTypeName.BOOL: (ClickhouseTypeName.BOOL, BooleanField(null=True)),
        ClickhouseTypeName.INT64: (ClickhouseTypeName.INT64, BigIntField(null=True)),
        ClickhouseTypeName.UINT64: (ClickhouseTypeName.UINT64, UInt64Field(null=True)),
        ClickhouseTypeName.INT128: (ClickhouseTypeName.INT128, Int128Field(null=True)),
        ClickhouseTypeName.UINT128: (ClickhouseTypeName.UINT128, UInt128Field(null=True)),
        ClickhouseTypeName.INT256: (ClickhouseTypeName.INT256, Int256Field(null=True)),
        ClickhouseTypeName.UINT256: (ClickhouseTypeName.UINT256, UInt256Field(null=True)),
        ClickhouseTypeName.FLOAT64: (ClickhouseTypeName.FLOAT64, FloatField(null=True)),
        ClickhouseTypeName.STRING: (ClickhouseTypeName.STRING, TextField(null=True)),
        ClickhouseTypeName.UUID: (ClickhouseTypeName.UUID, UUIDField(null=True)),
        ClickhouseTypeName.DATE32: (ClickhouseTypeName.DATE32, DateField(null=True)),
        ClickhouseTypeName.DATETIME64: ("DateTime64(6, 'UTC')", DatetimeField(null=True)),
        ClickhouseTypeName.IPV4: (ClickhouseTypeName.IPV4, IPv4AddressField(null=True)),
        ClickhouseTypeName.IPV6: (ClickhouseTypeName.IPV6, IPAddressField(null=True)),
    }

    def __init__(self, max_types: int | None = None, **kwargs: Any) -> None:
        # Local import: the ClickHouse types read the dialect's constants, which build this module's dialect.
        from hare.dialects.clickhouse.types.constants import CLICKHOUSE_DYNAMIC_MAX_TYPES

        if max_types is not None and (
            type(max_types) is not int or not 0 <= max_types <= CLICKHOUSE_DYNAMIC_MAX_TYPES
        ):
            raise ConfigurationError(
                f"DynamicField(max_types=...) takes an int from 0 to {CLICKHOUSE_DYNAMIC_MAX_TYPES}, got "
                f"{ValueDescriptions.get_shown_value(max_types)}"
            )
        self.max_types = max_types
        super().__init__(**kwargs)

    def get_python_type(self) -> Any:
        return Any

    def get_lookups(self) -> dict[str, FieldLookup]:
        # Local import: the lookups build hare's SQL terms, whose modules import the fields.
        from hare.dialects.clickhouse.lookups.clickhouse_typed_value_lookups import ClickhouseTypedValueLookups
        from hare.dialects.clickhouse.lookups.constants import (
            CLICKHOUSE_DYNAMIC_ELEMENT_FUNCTION_NAME,
            CLICKHOUSE_DYNAMIC_TYPE_FUNCTION_NAME,
        )

        return ClickhouseTypedValueLookups(
            CLICKHOUSE_DYNAMIC_TYPE_FUNCTION_NAME,
            CLICKHOUSE_DYNAMIC_ELEMENT_FUNCTION_NAME,
            compares_number_families=True,
        ).get_lookups()

    def get_path_transform(self, segment: str) -> tuple[Callable[[Term], Term], Field[Any]] | None:
        path_field = self.PATH_FIELDS.get(segment)
        if path_field is None:
            return super().get_path_transform(segment)
        # Local import: hare's SQL terms import the fields package.
        from hare.dialects.clickhouse.lookups.constants import CLICKHOUSE_DYNAMIC_ELEMENT_FUNCTION_NAME
        from hare.sql.terms.functions.function import Function
        from hare.sql.terms.values.value_wrapper import ValueWrapper

        held_type, held_field = path_field

        def get_held_values(term: Term) -> Term:
            return Function(
                CLICKHOUSE_DYNAMIC_ELEMENT_FUNCTION_NAME, term, ValueWrapper(held_type, allow_parametrize=False)
            )

        return get_held_values, held_field
