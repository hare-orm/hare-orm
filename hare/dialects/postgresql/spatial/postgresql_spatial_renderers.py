from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.postgresql.spatial.constants import (
    POSTGIS_AGGREGATES,
    POSTGIS_BOUNDING_BOX_OPERATORS,
    POSTGIS_FUNCTIONS,
    POSTGIS_GEOGRAPHY_FUNCTIONS,
    POSTGIS_GEOGRAPHY_RELATIONS,
    POSTGIS_GEOGRAPHY_TYPE,
    POSTGIS_GEOMETRY_TYPE,
    POSTGIS_RELATION_FUNCTIONS,
)
from hare.exceptions import UnSupportedError
from hare.gis.enums import SpatialFunctionType
from hare.gis.terms.geometry_value import GeometryValue
from hare.gis.terms.spatial_aggregate_function import SpatialAggregateFunction
from hare.gis.terms.spatial_function_term import SpatialFunctionTerm
from hare.gis.terms.spatial_relation_term import SpatialRelationTerm
from hare.sql.terms.functions.function import Function

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.sql.sql_context import SqlContext


class PostgresqlSpatialRenderers:
    """How PostGIS writes the spatial terms."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the spatial renderers on PostgreSQL's renderers.

        Args:
            renderers: The renderers.
        """
        renderers.register(GeometryValue, cls.render_geometry_value)
        renderers.register(SpatialRelationTerm, cls.render_spatial_relation)
        renderers.register(SpatialFunctionTerm, cls.render_spatial_function)
        renderers.register_name(SpatialAggregateFunction, cls.get_aggregate_name)
        renderers.register(SpatialAggregateFunction, cls.render_spatial_aggregate)

    @staticmethod
    def render_geometry_value(value: GeometryValue, sql_context: SqlContext) -> str:
        """The EWKT parameter read as the column's type - transformed to its SRID when it has another."""
        value_sql = Function.get_arg_sql(value.args[0], sql_context)
        if value.srid == value.target_srid:
            column_type = POSTGIS_GEOGRAPHY_TYPE if value.geography else POSTGIS_GEOMETRY_TYPE
            return f"CAST({value_sql} AS {column_type})"
        transformed_sql = f"ST_Transform(CAST({value_sql} AS {POSTGIS_GEOMETRY_TYPE}),{value.target_srid})"
        return f"CAST({transformed_sql} AS {POSTGIS_GEOGRAPHY_TYPE})" if value.geography else transformed_sql

    @staticmethod
    def render_spatial_relation(term: SpatialRelationTerm, sql_context: SqlContext) -> str:
        """``ST_<Relation>(a,b[,argument])``, or a bounding-box operator.

        Raises:
            UnSupportedError: The relation is planar and the column a geography.
        """
        if term.geography and term.relation not in POSTGIS_GEOGRAPHY_RELATIONS:
            raise UnSupportedError(
                f"The {term.relation.value} lookup tests geometry columns, not a geography - "
                f"PostGIS tests {sorted(relation.value for relation in POSTGIS_GEOGRAPHY_RELATIONS)} on one"
            )
        arguments_sql = [Function.get_arg_sql(argument, sql_context) for argument in term.args]
        bounding_box_operator = POSTGIS_BOUNDING_BOX_OPERATORS.get(term.relation)
        if bounding_box_operator is not None:
            return f"({arguments_sql[0]} {bounding_box_operator} {arguments_sql[1]})"
        return f"{POSTGIS_RELATION_FUNCTIONS[term.relation]}({','.join(arguments_sql)})"

    @staticmethod
    def render_spatial_function(term: SpatialFunctionTerm, sql_context: SqlContext) -> str:
        """``ST_<Function>(...)`` - a geography read as a geometry by a function without a geography
        form, ``ST_AsGeoJSON`` read as ``jsonb``."""
        as_geometry = term.geography and term.function_type not in POSTGIS_GEOGRAPHY_FUNCTIONS
        arguments_sql = []
        for index, argument in enumerate(term.args):
            argument_sql = Function.get_arg_sql(argument, sql_context)
            if as_geometry and index < term.geometry_argument_count:
                argument_sql = f"CAST({argument_sql} AS {POSTGIS_GEOMETRY_TYPE})"
            elif term.function_type == SpatialFunctionType.TRANSFORM and index == 1:
                # ST_Transform also takes a PROJ text - an uncast SRID parameter would be read as one.
                argument_sql = f"CAST({argument_sql} AS integer)"
            arguments_sql.append(argument_sql)
        function_sql = f"{POSTGIS_FUNCTIONS[term.function_type]}({','.join(arguments_sql)})"
        if term.function_type == SpatialFunctionType.AS_GEO_JSON:
            return f"CAST({function_sql} AS jsonb)"
        return function_sql

    @staticmethod
    def render_spatial_aggregate(function: SpatialAggregateFunction, sql_context: SqlContext) -> str:
        """The aggregate - ``ST_Extent``'s ``box2d`` read as its text, which has no binary form."""
        function_sql = function.get_function_sql(sql_context)
        if function.function_type == SpatialFunctionType.EXTENT:
            return f"CAST({function_sql} AS text)"
        return function_sql

    @staticmethod
    def get_aggregate_name(function: SpatialAggregateFunction, sql_context: SqlContext) -> str:
        """The PostGIS aggregate of a spatial aggregate."""
        return POSTGIS_AGGREGATES[function.function_type]
