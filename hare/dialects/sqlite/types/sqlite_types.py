from __future__ import annotations

import datetime
import math
from typing import Any

from hare.dialects.base.types.type_mapping import TypeMapping
from hare.dialects.base.types.type_registry import TypeRegistry
from hare.dialects.sqlite.spatial.sqlite_geometry_values import SqliteGeometryValues
from hare.dialects.sqlite.vectors.sqlite_vector_values import SqliteVectorValues
from hare.exceptions import ValidationError
from hare.fields.data.boolean_field import BooleanField
from hare.fields.data.json.json_field import JSONField
from hare.fields.data.numeric.float_field import FloatField
from hare.fields.data.numeric.int_field import IntField
from hare.fields.field import Field
from hare.gis.fields.extent_field import ExtentField
from hare.gis.fields.geometry_field import GeometryField
from hare.vectors.vector_field import VectorField


class SqliteTypes:
    """How SQLite stores hare's fields."""

    @staticmethod
    def to_db_float(field: Field[Any], value: Any, instance: Any) -> Any:
        """A float written - NaN refused: SQLite stores it as NULL.

        Raises:
            ValidationError: The value is NaN.
        """
        value = field.to_db_value(value, instance)
        if isinstance(value, float) and math.isnan(value):
            raise ValidationError(f"{field.model_field_name}: value is NaN, which SQLite stores as NULL")
        return value

    @classmethod
    def build(cls) -> TypeRegistry:
        """The SQLite type registry.

        Returns:
            The registry.
        """
        types = TypeRegistry()
        types.register(IntField, TypeMapping(generated_sql="INTEGER"))
        types.register(BooleanField, TypeMapping(column_type="INT"))
        types.register(FloatField, TypeMapping(column_type="REAL", to_db=cls.to_db_float, to_db_refuses_only=True))
        # TEXT affinity (the declared type contains "TEXT") - a plain "JSON" column gets NUMERIC
        # affinity, which turns a stored top-level number into INTEGER/REAL.
        types.register(JSONField, TypeMapping(column_type="JSON_TEXT"))
        types.register(
            GeometryField,
            TypeMapping(
                column_type=SqliteGeometryValues.get_column_type,
                to_db=SqliteGeometryValues.to_db,
                to_lookup=SqliteGeometryValues.to_lookup,
                to_python=SqliteGeometryValues.to_python,
            ),
        )
        types.register(ExtentField, TypeMapping(to_python=SqliteGeometryValues.to_python_extent))
        types.register(
            VectorField,
            TypeMapping(
                column_type=SqliteVectorValues.get_column_type,
                to_db=SqliteVectorValues.to_db,
                to_lookup=SqliteVectorValues.to_db,
                to_python=SqliteVectorValues.to_python,
            ),
        )
        # The parameter adapters write these as text.
        types.bound_as_text = frozenset({datetime.datetime, datetime.date, datetime.time})
        return types
