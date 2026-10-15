from __future__ import annotations

import datetime
from typing import Any, cast

from hare.dialects.base.types.type_mapping import TypeMapping
from hare.dialects.base.types.type_registry import TypeRegistry
from hare.dialects.postgresql.fields.constants import POSTGRESQL_INET_TYPE
from hare.dialects.postgresql.spatial.constants import (
    POSTGIS_EXTENSION,
    POSTGIS_GEOGRAPHY_TYPE,
    POSTGIS_GEOMETRY_TYPE,
    POSTGIS_GEOMETRY_TYPE_NAMES,
)
from hare.dialects.postgresql.types.postgresql_container_values import PostgresqlContainerValues
from hare.dialects.postgresql.vectors.constants import PGVECTOR_EXTENSION
from hare.dialects.postgresql.vectors.postgresql_vector_values import PostgresqlVectorValues
from hare.fields.constants import NAIVE_INFINITY_DATETIMES
from hare.fields.data.binary_field import BinaryField
from hare.fields.data.containers.array_field import ArrayField
from hare.fields.data.json.json_field import JSONField
from hare.fields.data.network.ip_address_field import IPAddressField
from hare.fields.data.numeric.big_int_field import BigIntField
from hare.fields.data.numeric.int_field import IntField
from hare.fields.data.numeric.small_int_field import SmallIntField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.data.uuid_field import UUIDField
from hare.fields.field import Field
from hare.gis.fields.geometry_field import GeometryField
from hare.vectors.vector_field import VectorField


class PostgresqlTypes:
    """How PostgreSQL stores hare's fields."""

    @staticmethod
    def build() -> TypeRegistry:
        """The PostgreSQL type registry.

        Returns:
            The registry.
        """
        types = TypeRegistry()
        # A NULL element is held by an array of any element type.
        types.container_values_always_nullable = True
        container_values = PostgresqlContainerValues(types)
        types.register(
            ArrayField,
            TypeMapping(
                column_type=container_values.get_column_type,
                to_db=container_values.to_db,
                to_lookup=container_values.to_db,
                to_python=container_values.to_python,
            ),
        )
        types.register(IntField, TypeMapping(generated_sql="SERIAL NOT NULL PRIMARY KEY"))
        types.register(BigIntField, TypeMapping(generated_sql="BIGSERIAL NOT NULL PRIMARY KEY"))
        types.register(SmallIntField, TypeMapping(generated_sql="SMALLSERIAL NOT NULL PRIMARY KEY"))
        types.register(
            DatetimeField,
            TypeMapping(
                column_type="TIMESTAMPTZ",
                to_db=DatetimeField.to_db_instant_value,
                to_lookup=DatetimeField.to_db_instant_value,
                to_python=PostgresqlTypes.get_datetime_python_value,
                naive_datetime_is_utc=True,
            ),
        )
        types.register(TimeField, TypeMapping(column_type="TIMETZ"))
        types.register(JSONField, TypeMapping(column_type="JSONB"))
        types.register(UUIDField, TypeMapping(column_type="UUID"))
        types.register(IPAddressField, TypeMapping(column_type=POSTGRESQL_INET_TYPE))
        types.register(BinaryField, TypeMapping(column_type="BYTEA"))
        types.register(
            GeometryField,
            TypeMapping(column_type=PostgresqlTypes.get_geometry_column_type, extension=POSTGIS_EXTENSION),
        )
        types.register(
            VectorField,
            TypeMapping(
                column_type=PostgresqlVectorValues.get_column_type,
                to_db=PostgresqlVectorValues.to_db,
                to_lookup=PostgresqlVectorValues.to_db,
                to_python=PostgresqlVectorValues.to_python,
                extension=PGVECTOR_EXTENSION,
            ),
        )
        return types

    @staticmethod
    def get_geometry_column_type(field: Field[Any]) -> str:
        """The PostGIS column of a geometry field - ``geometry(PointZ,4326)``, ``geography(Polygon,4326)``.

        Args:
            field: The field.

        Returns:
            The column type.
        """
        geometry_field = cast("GeometryField", field)
        column_type = POSTGIS_GEOGRAPHY_TYPE if geometry_field.geography else POSTGIS_GEOMETRY_TYPE
        type_name = POSTGIS_GEOMETRY_TYPE_NAMES[geometry_field.get_geometry_type()]
        dimension_suffix = "Z" if geometry_field.dimensions == 3 else ""
        return f"{column_type}({type_name}{dimension_suffix},{geometry_field.srid})"

    @staticmethod
    def get_datetime_python_value(field: DatetimeField[Any], value: Any) -> Any:
        """Reads a datetime column; a naive value comes from a ``timestamp`` column, whose wall
        clock is UTC in hare's UTC sessions.

        Args:
            field: The field.
            value: The value the driver returned.

        Returns:
            The Python value.
        """
        if isinstance(value, datetime.datetime) and value.tzinfo is None and value not in NAIVE_INFINITY_DATETIMES:
            value = value.replace(tzinfo=datetime.UTC)
        return field.from_db_value(value)
