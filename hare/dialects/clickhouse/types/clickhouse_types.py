from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.dialects.base.types.type_mapping import TypeMapping
from hare.dialects.base.types.type_registry import TypeRegistry
from hare.dialects.clickhouse.constants import CLICKHOUSE_DATETIME_COLUMN_TYPE
from hare.dialects.clickhouse.fields.dynamic_field import DynamicField
from hare.dialects.clickhouse.fields.float32_field import Float32Field
from hare.dialects.clickhouse.fields.low_cardinality_field import LowCardinalityField
from hare.dialects.clickhouse.fields.variant_field import VariantField
from hare.dialects.clickhouse.spatial.clickhouse_geometry_values import ClickhouseGeometryValues
from hare.dialects.clickhouse.types.clickhouse_address_values import ClickhouseAddressValues
from hare.dialects.clickhouse.types.clickhouse_container_values import ClickhouseContainerValues
from hare.dialects.clickhouse.types.clickhouse_dynamic_values import ClickhouseDynamicValues
from hare.dialects.clickhouse.types.clickhouse_enum_types import ClickhouseEnumTypes
from hare.dialects.clickhouse.types.clickhouse_json_values import ClickhouseJsonValues
from hare.dialects.clickhouse.types.clickhouse_low_cardinality_values import ClickhouseLowCardinalityValues
from hare.dialects.clickhouse.types.clickhouse_variant_values import ClickhouseVariantValues
from hare.fields.data.binary_field import BinaryField
from hare.fields.data.choices.char_enum_field_instance import CharEnumFieldInstance
from hare.fields.data.choices.int_enum_field_instance import IntEnumFieldInstance
from hare.fields.data.containers.container_field import ContainerField
from hare.fields.data.json.json_field import JSONField
from hare.fields.data.network.ip_address_field import IPAddressField
from hare.fields.data.network.ipv4_address_field import IPv4AddressField
from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.data.numeric.float_field import FloatField
from hare.fields.data.temporal.date_field import DateField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.data.uuid_field import UUIDField
from hare.fields.field import Field
from hare.gis.fields.geometry_field import GeometryField

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.clickhouse.clickhouse_dialect import ClickhouseDialect
    from hare.models import Model


class ClickhouseTypes:
    """How ClickHouse stores hare's fields. The ISO names of the other fields' types (``INT``,
    ``BIGINT``, ``VARCHAR(n)``, ``TEXT``, ``BOOL``) are ClickHouse's aliases of its own types."""

    @staticmethod
    def get_decimal_column_type(field: Field[Any]) -> str:
        """``Decimal(P, S)`` of the field's digits.

        Args:
            field: The decimal field.

        Returns:
            The column type.
        """
        decimal_field = cast("DecimalField[Any]", field)
        return f"Decimal({decimal_field.max_digits}, {decimal_field.decimal_places})"

    @staticmethod
    def get_float32_lookup_value(field: Field[Any], value: Any, instance: type[Model] | Model | None) -> Any:
        """A value a ``Float32`` column is compared with, as a float32 - a float64 of the same text is
        another number (``0.1`` isn't the float32 ``0.1``).

        Args:
            field: The float32 field.
            value: The Python value.
            instance: The model (class) the filter is built for.

        Returns:
            The value as ``toFloat32()`` reads it.
        """
        # Local import: hare.sql's terms import the dialects package.
        from hare.sql.terms.functions.function import Function
        from hare.sql.terms.values.value_wrapper import ValueWrapper

        db_value = field.to_db_value(value, instance)  # type: ignore[arg-type]
        return None if db_value is None else Function("toFloat32", ValueWrapper(db_value))

    @classmethod
    def build(cls, dialect: ClickhouseDialect) -> TypeRegistry:
        """The ClickHouse type registry.

        Args:
            dialect: The dialect the types are of - storing JSON natively or as text.

        Returns:
            The registry.
        """
        types = TypeRegistry()
        # TIMESTAMP is a DateTime of whole seconds, DATE one up to 2149 only.
        types.register(
            DatetimeField, TypeMapping(column_type=CLICKHOUSE_DATETIME_COLUMN_TYPE, naive_datetime_is_utc=True)
        )
        types.register(DateField, TypeMapping(column_type="Date32"))
        # DOUBLE PRECISION is an alias too, but ClickHouse 24.3 doesn't read its two words inside
        # Nullable(...).
        types.register(FloatField, TypeMapping(column_type="Float64"))
        # A time of day is ISO text - compared and ordered as the text of one width.
        types.register(TimeField, TypeMapping(column_type="String"))
        # Written as the UUID itself: a binary insert takes it as it is, a statement as toUUID().
        types.register(UUIDField, TypeMapping(column_type="UUID", to_db=UUIDField.to_db_uuid))
        types.register(DecimalField, TypeMapping(column_type=cls.get_decimal_column_type))
        if dialect.stores_json_natively:
            types.register(
                JSONField,
                TypeMapping(column_type="JSON", to_db=ClickhouseJsonValues.to_db),
            )
        else:
            # JSON text - on a server without the JSON type.
            types.register(JSONField, TypeMapping(column_type="String"))
        types.register(BinaryField, TypeMapping(column_type="String"))
        types.register(Float32Field, TypeMapping(column_type="Float32", to_lookup=cls.get_float32_lookup_value))
        types.register(
            IPAddressField,
            TypeMapping(
                column_type="IPv6", to_db=ClickhouseAddressValues.to_db, to_lookup=ClickhouseAddressValues.to_db
            ),
        )
        types.register(IPv4AddressField, TypeMapping(column_type="IPv4"))
        low_cardinality_values = ClickhouseLowCardinalityValues(types, dialect)
        types.register(
            LowCardinalityField,
            TypeMapping(
                column_type=low_cardinality_values.get_column_type,
                to_db=low_cardinality_values.to_db,
                to_lookup=low_cardinality_values.to_lookup,
                to_python=low_cardinality_values.to_python,
            ),
        )
        types.register(CharEnumFieldInstance, TypeMapping(column_type=ClickhouseEnumTypes.get_char_enum_column_type))
        types.register(
            IntEnumFieldInstance,
            TypeMapping(
                column_type=ClickhouseEnumTypes.get_int_enum_column_type,
                to_db=ClickhouseEnumTypes.get_int_enum_db_value,
                to_lookup=ClickhouseEnumTypes.get_int_enum_db_value,
                to_python=ClickhouseEnumTypes.get_int_enum_python_value,
            ),
        )
        types.register(
            DynamicField,
            TypeMapping(
                column_type=ClickhouseDynamicValues.get_column_type,
                to_db=ClickhouseDynamicValues.to_db,
                to_lookup=ClickhouseDynamicValues.to_lookup,
                to_python=ClickhouseDynamicValues.to_python,
                inserted_by_select=True,
            ),
        )
        variant_values = ClickhouseVariantValues(types, dialect)
        types.register(
            VariantField,
            TypeMapping(
                column_type=variant_values.get_column_type,
                to_db=variant_values.to_db,
                to_lookup=variant_values.to_lookup,
                to_python=variant_values.to_python,
                inserted_by_select=True,
            ),
        )
        types.register(
            GeometryField,
            TypeMapping(
                column_type=ClickhouseGeometryValues.get_column_type,
                to_db=ClickhouseGeometryValues.to_db,
                to_lookup=ClickhouseGeometryValues.to_lookup,
                to_python=ClickhouseGeometryValues.to_python,
            ),
        )
        container_values = ClickhouseContainerValues(types, dialect)
        types.null_container_value = container_values.get_empty_value
        types.register(
            ContainerField,
            TypeMapping(
                column_type=container_values.get_column_type,
                to_db=container_values.to_db,
                to_lookup=container_values.to_db,
                to_python=container_values.to_python,
                inserted_by_select=ClickhouseContainerValues.holds_typed_values,
            ),
        )
        return types
