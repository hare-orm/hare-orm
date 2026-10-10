from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from hare.dialects.base.types.type_mapping import TypeMapping
from hare.dialects.base.types.type_registry import TypeRegistry
from hare.dialects.sqlite.constants import SQLITE_DECIMAL_COLLATION_NAME, SQLITE_TIME_COLLATION_NAME
from hare.dialects.sqlite.spatial.sqlite_geometry_values import SqliteGeometryValues
from hare.dialects.sqlite.vectors.sqlite_vector_values import SqliteVectorValues
from hare.fields.data.boolean_field import BooleanField
from hare.fields.data.json.json_field import JSONField
from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.data.numeric.float_field import FloatField
from hare.fields.data.numeric.int_field import IntField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.field import Field
from hare.gis.fields.extent_field import ExtentField
from hare.gis.fields.geometry_field import GeometryField
from hare.sql import functions
from hare.sql.terms.term import Term
from hare.vectors.vector_field import VectorField


class SqliteTypes:
    """How SQLite stores hare's fields."""

    @staticmethod
    def collate_decimal(field: Field[Any], term: Term) -> Term:
        """A decimal column stays text: comparisons, ordering, Min/Max and a plain F() copy go
        through an exact-decimal collation instead of ``CAST(... AS NUMERIC)``, which would turn
        the value into a double."""
        return functions.DecimalTextCollate(term, SQLITE_DECIMAL_COLLATION_NAME)

    @staticmethod
    def collate_time(field: Field[Any], term: Term) -> Term:
        """A time is ISO text with its own offset - compared and ordered by the UTC time, as
        Postgres orders ``TIMETZ``."""
        return functions.Collate(term, SQLITE_TIME_COLLATION_NAME)

    @classmethod
    def build(cls) -> TypeRegistry:
        """The SQLite type registry.

        Returns:
            The registry.
        """
        types = TypeRegistry()
        types.register(IntField, TypeMapping(generated_sql="INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL"))
        types.register(BooleanField, TypeMapping(column_type="INT"))
        types.register(DecimalField, TypeMapping(column_type="VARCHAR(40)", function_cast=cls.collate_decimal))
        types.register(TimeField, TypeMapping(function_cast=cls.collate_time))
        types.register(FloatField, TypeMapping(column_type="REAL"))
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
        types.bound_as_text = frozenset({datetime.datetime, datetime.date, datetime.time, Decimal})
        return types
