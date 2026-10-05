from __future__ import annotations

from typing import TYPE_CHECKING, cast

from hare.dialects.sqlite.indexes.spatialite_index import SpatialiteIndex
from hare.dialects.sqlite.spatial.constants import (
    SPATIALITE_AGGREGATES,
    SPATIALITE_CONTAINS_PROPERLY_PATTERN,
    SPATIALITE_EXPANDED_FRAME_SQL,
    SPATIALITE_FUNCTIONS,
    SPATIALITE_GEOGRAPHY_MEASURES,
    SPATIALITE_GEOGRAPHY_RELATIONS,
    SPATIALITE_GEOMETRY_TYPE_Z_SUFFIX,
    SPATIALITE_INDEX_PREFILTER_SQL,
    SPATIALITE_INDEXED_RELATIONS,
    SPATIALITE_RELATION_FUNCTIONS,
    SPATIALITE_UNSUPPORTED_GEOGRAPHY_FUNCTIONS,
)
from hare.dialects.sqlite.spatial.spatialite_blob_writer import SpatialiteBlobWriter
from hare.exceptions import UnSupportedError
from hare.gis.enums import SpatialFunctionType, SpatialRelation
from hare.gis.terms.geometry_value import GeometryValue
from hare.gis.terms.spatial_aggregate_function import SpatialAggregateFunction
from hare.gis.terms.spatial_function_term import SpatialFunctionTerm
from hare.gis.terms.spatial_relation_term import SpatialRelationTerm
from hare.sql.terms.field import Field as SqlField
from hare.sql.terms.functions.function import Function
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.sql.sql_context import SqlContext


class SqliteSpatialRenderers:
    """How SpatiaLite writes the spatial terms. A geometry from Python is bound as SpatiaLite's own
    BLOB; a test SpatiaLite answers with ``-1`` for a NULL or broken geometry is read as unknown, as
    PostGIS's NULL; a geography is a longitude/latitude geometry measured on the ellipsoid."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the spatial renderers on SQLite's renderers.

        Args:
            renderers: The renderers.
        """
        renderers.register(GeometryValue, cls.render_geometry_value)
        renderers.register(SpatialRelationTerm, cls.render_spatial_relation)
        renderers.register(SpatialFunctionTerm, cls.render_spatial_function)
        renderers.register_name(SpatialAggregateFunction, cls.get_aggregate_name)

    @classmethod
    def render_geometry_value(cls, value: GeometryValue, sql_context: SqlContext) -> str:
        """The geometry's BLOB - transformed to the column's SRID when it has another."""
        value_sql = Function.get_arg_sql(ValueWrapper(SpatialiteBlobWriter.write(value.geometry)), sql_context)
        if value.srid == value.target_srid:
            return value_sql
        return cls.get_transform_sql(value_sql, str(value.srid), str(value.target_srid))

    @staticmethod
    def get_transform_sql(geometry_sql: str, source_srid_sql: str, target_srid_sql: str) -> str:
        """``ST_Transform`` with the SRIDs as EPSG codes - read by PROJ itself, so the database needs
        no ``spatial_ref_sys`` table.

        Args:
            geometry_sql: The geometry.
            source_srid_sql: The SQL of its SRID.
            target_srid_sql: The SQL of the SRID it is transformed to.

        Returns:
            The SQL.
        """
        return (
            f"ST_Transform({geometry_sql},{target_srid_sql},NULL,"
            f"'EPSG:'||({source_srid_sql}),'EPSG:'||({target_srid_sql}))"
        )

    @staticmethod
    def get_test_sql(function_sql: str) -> str:
        """A SpatiaLite test - ``1``, ``0``, or ``-1`` for a NULL or broken geometry - as true, false
        or unknown."""
        return f"CASE {function_sql} WHEN 1 THEN 1 WHEN 0 THEN 0 END"

    @classmethod
    def render_spatial_relation(cls, term: SpatialRelationTerm, sql_context: SqlContext) -> str:
        """``ST_<Relation>(a,b[,pattern])``, a bounding-box test, or - for ``dwithin`` - the distance
        compared.

        Raises:
            UnSupportedError: The column is a geography and the relation isn't measured on the
                ellipsoid.
        """
        if term.geography and term.relation not in SPATIALITE_GEOGRAPHY_RELATIONS:
            raise UnSupportedError(
                f"The {term.relation.value} lookup on a geography needs the spheroid PostGIS tests it on - "
                f"SpatiaLite tests {sorted(relation.value for relation in SPATIALITE_GEOGRAPHY_RELATIONS)} "
                "and the distance lookups on one"
            )
        # Rendered first - its parameters come first in the SQL.
        prefilter_sql = cls.get_index_prefilter_sql(term, sql_context)
        arguments_sql = [Function.get_arg_sql(argument, sql_context) for argument in term.args]
        if term.relation == SpatialRelation.DWITHIN:
            distance_sql = cls.get_distance_sql(arguments_sql[0], arguments_sql[1], term.geography)
            test_sql = f"({distance_sql} <= {arguments_sql[2]})"
        elif term.relation == SpatialRelation.CONTAINS_PROPERLY:
            pattern_sql = Function.get_arg_sql(ValueWrapper(SPATIALITE_CONTAINS_PROPERLY_PATTERN), sql_context)
            test_sql = cls.get_test_sql(f"ST_Relate({arguments_sql[0]},{arguments_sql[1]},{pattern_sql})")
        else:
            function_name = SPATIALITE_RELATION_FUNCTIONS[term.relation]
            test_sql = cls.get_test_sql(f"{function_name}({','.join(arguments_sql)})")
        return test_sql if prefilter_sql is None else f"({prefilter_sql} AND {test_sql})"

    @staticmethod
    def get_index_prefilter_sql(term: SpatialRelationTerm, sql_context: SqlContext) -> str | None:
        """The rows the relation can hold for, by the column's ``SpatialiteIndex`` - those whose
        bounding box meets the other geometry's (grown by ``dwithin``'s distance).

        Args:
            term: The relation.
            sql_context: The SQL context.

        Returns:
            The condition; None when the column has no spatial index or the relation can hold
            outside the other geometry's bounding box - ``disjoint``, ``relate``, a geography's
            ``dwithin`` measured in meters.
        """
        field = term.field
        column = term.args[0]
        if (
            field is None
            or term.relation not in SPATIALITE_INDEXED_RELATIONS
            or (term.geography and term.relation == SpatialRelation.DWITHIN)
            or not isinstance(column, SqlField)
            or column.table is None
        ):
            return None
        model = field.model
        if SpatialiteIndex.get_covering(model, field.model_field_name) is None:
            return None
        row_key = SqlField(
            model._meta.get_column_names([cast("str", model._meta.primary_key_attribute)])[0], table=column.table
        )
        row_key_sql = Function.get_arg_sql(row_key, sql_context)
        table_sql = Function.get_arg_sql(ValueWrapper(model._meta.db_table), sql_context)
        column_sql = Function.get_arg_sql(ValueWrapper(column.name), sql_context)
        frame_sql = Function.get_arg_sql(term.args[1], sql_context)
        if term.relation == SpatialRelation.DWITHIN:
            frame_sql = SPATIALITE_EXPANDED_FRAME_SQL.format(
                geometry=frame_sql, distance=Function.get_arg_sql(term.args[2], sql_context)
            )
        return SPATIALITE_INDEX_PREFILTER_SQL.format(
            row_key=row_key_sql, table=table_sql, column=column_sql, frame=frame_sql
        )

    @staticmethod
    def get_distance_sql(first_sql: str, second_sql: str, geography: bool) -> str:
        """``ST_Distance`` - on the ellipsoid, in meters, for a geography."""
        ellipsoid_sql = ",1" if geography else ""
        return f"ST_Distance({first_sql},{second_sql}{ellipsoid_sql})"

    @classmethod
    def render_spatial_function(cls, term: SpatialFunctionTerm, sql_context: SqlContext) -> str:
        """``ST_<Function>(...)`` - a measure of a geography on the ellipsoid, ``ST_Transform`` with EPSG
        codes, ``GeometryType`` without SpatiaLite's `` Z``.

        Raises:
            UnSupportedError: The geometry is a geography and the function is one PostGIS computes on
                the spheroid, which SpatiaLite has no form of.
        """
        function_type = term.function_type
        if term.geography and function_type in SPATIALITE_UNSUPPORTED_GEOGRAPHY_FUNCTIONS:
            raise UnSupportedError(
                f"{function_type.value} of a geography is computed on the spheroid by PostGIS - SpatiaLite "
                "has no such form; transform the geography to a planar SRID first"
            )
        if function_type == SpatialFunctionType.TRANSFORM:
            return cls.get_transformed_term_sql(term, sql_context)
        arguments_sql = [Function.get_arg_sql(argument, sql_context) for argument in term.args]
        if term.geography and function_type in SPATIALITE_GEOGRAPHY_MEASURES:
            arguments_sql.append("1")
        function_sql = f"{SPATIALITE_FUNCTIONS[function_type]}({','.join(arguments_sql)})"
        if function_type == SpatialFunctionType.IS_VALID:
            return cls.get_test_sql(function_sql)
        if function_type == SpatialFunctionType.GEOMETRY_TYPE_NAME:
            return f"REPLACE({function_sql},'{SPATIALITE_GEOMETRY_TYPE_Z_SUFFIX}','')"
        return function_sql

    @staticmethod
    def get_transformed_term_sql(term: SpatialFunctionTerm, sql_context: SqlContext) -> str:
        """``Transform(geometry, srid)`` - the geometry and the SRID are each written twice, in the order
        of the SQL text, so a bound value is bound at each place it is read."""
        geometry, srid = term.args
        geometry_sql = Function.get_arg_sql(geometry, sql_context)
        srid_sql = Function.get_arg_sql(srid, sql_context)
        source_reference_sql = f"'EPSG:'||ST_SRID({Function.get_arg_sql(geometry, sql_context)})"
        target_reference_sql = f"'EPSG:'||({Function.get_arg_sql(srid, sql_context)})"
        return f"ST_Transform({geometry_sql},{srid_sql},NULL,{source_reference_sql},{target_reference_sql})"

    @staticmethod
    def get_aggregate_name(function: SpatialAggregateFunction, sql_context: SqlContext) -> str:
        """The SpatiaLite aggregate of a spatial aggregate."""
        return SPATIALITE_AGGREGATES[function.function_type]
