from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.clickhouse.spatial.clickhouse_geometry_values import ClickhouseGeometryValues
from hare.dialects.clickhouse.spatial.constants import (
    CLICKHOUSE_AREA_DISTANCE_FUNCTIONS,
    CLICKHOUSE_AREA_FUNCTIONS,
    CLICKHOUSE_AREA_GEOMETRY_TYPES,
    CLICKHOUSE_AREA_RELATION_FUNCTIONS,
    CLICKHOUSE_AS_TEXT_FUNCTION,
    CLICKHOUSE_COORDINATE_ELEMENTS,
    CLICKHOUSE_EARTH_RADIUS_METERS,
    CLICKHOUSE_LEFT_INSIDE_RELATIONS,
    CLICKHOUSE_POINT_DISTANCE_FUNCTIONS,
    CLICKHOUSE_POINT_IN_AREA_RELATIONS,
    CLICKHOUSE_POINT_IN_POLYGON_FUNCTION,
)
from hare.exceptions import UnSupportedError
from hare.gis.enums import GeometryType, SpatialFunctionType, SpatialRelation
from hare.gis.terms.geometry_value import GeometryValue
from hare.gis.terms.spatial_aggregate_function import SpatialAggregateFunction
from hare.gis.terms.spatial_function_term import SpatialFunctionTerm
from hare.gis.terms.spatial_relation_term import SpatialRelationTerm
from hare.sql.terms.functions.function import Function
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.gis.fields.geometry_field import GeometryField
    from hare.sql.sql_context import SqlContext


class ClickhouseSpatialRenderers:
    """How ClickHouse writes the spatial terms - with its functions of points and areas (polygons and
    multipolygons), chosen by the geometries' types: a point in an area (``pointInPolygon``, its
    border counting as in it), relations and measures of areas (``polygons*``, ``polygonArea*``), the
    distance of points (``L2Distance``; ``geoDistance`` in meters on the WGS 84 ellipsoid for a
    geography). A geography is measured on a sphere of WGS 84's mean radius. What ClickHouse has no
    function of is refused."""

    @classmethod
    def register(cls, renderers: TermRenderers) -> None:
        """Registers the spatial renderers on ClickHouse's renderers.

        Args:
            renderers: The renderers.
        """
        renderers.register(GeometryValue, cls.render_geometry_value)
        renderers.register(SpatialRelationTerm, cls.render_spatial_relation)
        renderers.register(SpatialFunctionTerm, cls.render_spatial_function)
        renderers.register(SpatialAggregateFunction, cls.render_spatial_aggregate)

    @staticmethod
    def render_geometry_value(value: GeometryValue, sql_context: SqlContext) -> str:
        """The geometry as its ClickHouse type's value.

        Raises:
            UnSupportedError: The geometry has another SRID than the column - ClickHouse transforms none.
        """
        if value.srid != value.target_srid:
            raise UnSupportedError(
                f"ClickHouse transforms no geometry between reference systems - give it in SRID {value.target_srid}, "
                f"not {value.srid}"
            )
        return Function.get_arg_sql(
            ValueWrapper(ClickhouseGeometryValues.get_typed_value(value.geometry)), sql_context
        )

    @staticmethod
    def get_geometry_type(argument: Any, field: GeometryField | None) -> GeometryType:
        """The type of a geometry argument - a value's own, else its field's.

        Args:
            argument: The argument.
            field: Its field, None when it has none.

        Returns:
            The type.

        Raises:
            UnSupportedError: Neither tells it.
        """
        if isinstance(argument, GeometryValue):
            return argument.geometry.GEOMETRY_TYPE
        if field is None:
            raise UnSupportedError(
                "ClickHouse writes a spatial test by the types of its geometries - a geometry of no geometry field "
                "has none"
            )
        return field.get_geometry_type()

    @staticmethod
    def get_spherical_measure_sql(function_sql: str, radius_power: int) -> str:
        """A measure taken on the sphere of radius one in meters - multiplied by the earth's radius.

        Args:
            function_sql: The measure.
            radius_power: 1 for a length, 2 for an area.

        Returns:
            The SQL.
        """
        return f"({function_sql} * {CLICKHOUSE_EARTH_RADIUS_METERS**radius_power!r})"

    @classmethod
    def get_distance_sql(
        cls, left_sql: str, left_type: GeometryType, right_sql: str, right_type: GeometryType, spherical: bool
    ) -> str:
        """The distance of two geometries - of points, or of a point or an area and an area.

        Raises:
            UnSupportedError: A geometry is a line.
        """
        if left_type == GeometryType.POINT and right_type == GeometryType.POINT:
            if not spherical:
                return f"{CLICKHOUSE_POINT_DISTANCE_FUNCTIONS[0]}({left_sql},{right_sql})"
            return (
                f"{CLICKHOUSE_POINT_DISTANCE_FUNCTIONS[1]}(tupleElement({left_sql},1),tupleElement({left_sql},2),"
                f"tupleElement({right_sql},1),tupleElement({right_sql},2))"
            )
        measured_types = (GeometryType.POINT, *CLICKHOUSE_AREA_GEOMETRY_TYPES)
        if left_type not in measured_types or right_type not in measured_types:
            raise UnSupportedError(
                f"ClickHouse measures the distance of points and areas, not of a {left_type.value} and a "
                f"{right_type.value}"
            )
        # A point is the polygon of one point.
        left_area_sql = f"[[{left_sql}]]" if left_type == GeometryType.POINT else left_sql
        right_area_sql = f"[[{right_sql}]]" if right_type == GeometryType.POINT else right_sql
        distance_sql = f"{CLICKHOUSE_AREA_DISTANCE_FUNCTIONS[spherical]}({left_area_sql},{right_area_sql})"
        return cls.get_spherical_measure_sql(distance_sql, 1) if spherical else distance_sql

    @classmethod
    def render_spatial_relation(cls, term: SpatialRelationTerm, sql_context: SqlContext) -> str:
        """The relation of two geometries.

        Raises:
            UnSupportedError: ClickHouse has no test of the relation for the geometries' types.
        """
        relation = term.relation
        left, right = term.args[0], term.args[1]
        left_type = cls.get_geometry_type(left, term.field)
        right_type = cls.get_geometry_type(right, None)
        left_sql = Function.get_arg_sql(left, sql_context)
        right_sql = Function.get_arg_sql(right, sql_context)
        if relation == SpatialRelation.DWITHIN:
            distance_sql = cls.get_distance_sql(left_sql, left_type, right_sql, right_type, term.geography)
            within_sql = f"({distance_sql} <= {Function.get_arg_sql(term.args[2], sql_context)})"
            if left_type == GeometryType.POINT or isinstance(left, GeometryValue):
                return within_sql
            return f"(notEmpty({left_sql}) AND {within_sql})"
        test_sql = None
        if relation in CLICKHOUSE_POINT_IN_AREA_RELATIONS:
            if left_type == GeometryType.POINT and right_type in CLICKHOUSE_AREA_GEOMETRY_TYPES:
                if relation not in {SpatialRelation.CONTAINS, SpatialRelation.COVERS}:
                    test_sql = f"{CLICKHOUSE_POINT_IN_POLYGON_FUNCTION}({left_sql},{right_sql})"
            elif (
                left_type in CLICKHOUSE_AREA_GEOMETRY_TYPES
                and right_type == GeometryType.POINT
                and relation not in CLICKHOUSE_LEFT_INSIDE_RELATIONS
            ):
                test_sql = f"{CLICKHOUSE_POINT_IN_POLYGON_FUNCTION}({right_sql},{left_sql})"
        area_functions = CLICKHOUSE_AREA_RELATION_FUNCTIONS.get(relation)
        if (
            test_sql is None
            and area_functions is not None
            and left_type in CLICKHOUSE_AREA_GEOMETRY_TYPES
            and right_type in CLICKHOUSE_AREA_GEOMETRY_TYPES
        ):
            function_name = area_functions[1] if term.geography else area_functions[0]
            if function_name is None:
                raise UnSupportedError(
                    f"ClickHouse tests the {relation.value} of areas on a plane only, not of a geography"
                )
            inner_sql, outer_sql = (
                (right_sql, left_sql)
                if relation in {SpatialRelation.CONTAINS, SpatialRelation.COVERS}
                else (left_sql, right_sql)
            )
            test_sql = f"{function_name}({inner_sql},{outer_sql})"
        if (
            test_sql is None
            and relation in {SpatialRelation.EQUALS, SpatialRelation.INTERSECTS, SpatialRelation.DISJOINT}
            and left_type == right_type
            and left_type not in CLICKHOUSE_AREA_GEOMETRY_TYPES
        ):
            # Points and lines meet as the same coordinates.
            test_sql = f"({left_sql} = {right_sql})"
        if test_sql is None:
            raise UnSupportedError(
                f"ClickHouse has no test of {relation.value} for a {left_type.value} and a {right_type.value}"
            )
        if relation == SpatialRelation.DISJOINT:
            test_sql = f"NOT {test_sql}"
        if left_type == GeometryType.POINT or isinstance(left, GeometryValue):
            return test_sql
        # An empty line or area - a NULL one - stands in no relation, as PostGIS's empty geometry.
        return f"(notEmpty({left_sql}) AND {test_sql})"

    @classmethod
    def render_spatial_function(cls, term: SpatialFunctionTerm, sql_context: SqlContext) -> str:
        """A spatial function - a coordinate of a point, a measure, an area's own functions, the
        well-known text; the SRID is the field's own.

        Raises:
            UnSupportedError: ClickHouse has no such function, or none for the geometries' types.
        """
        function_type = term.function_type
        geometry_fields = [
            term.geometry_fields[index] if index < len(term.geometry_fields) else None
            for index in range(term.geometry_argument_count)
        ]
        if function_type == SpatialFunctionType.SRID:
            field = geometry_fields[0]
            if field is None:
                raise UnSupportedError("ClickHouse keeps no SRID - only a geometry field's own one is read")
            return f"toInt32({field.srid})"
        geometry_types = [
            cls.get_geometry_type(argument, field)
            for argument, field in zip(term.args[: term.geometry_argument_count], geometry_fields, strict=True)
        ]
        arguments_sql = [Function.get_arg_sql(argument, sql_context) for argument in term.args]
        coordinate_element = CLICKHOUSE_COORDINATE_ELEMENTS.get(function_type)
        if coordinate_element is not None and geometry_types[0] == GeometryType.POINT:
            return f"tupleElement({arguments_sql[0]},{coordinate_element})"
        if function_type == SpatialFunctionType.AS_TEXT:
            return f"{CLICKHOUSE_AS_TEXT_FUNCTION}({arguments_sql[0]})"
        if function_type == SpatialFunctionType.DISTANCE:
            return cls.get_distance_sql(
                arguments_sql[0], geometry_types[0], arguments_sql[1], geometry_types[1], term.geography
            )
        area_function = CLICKHOUSE_AREA_FUNCTIONS.get(function_type)
        if area_function is not None and all(
            geometry_type in CLICKHOUSE_AREA_GEOMETRY_TYPES for geometry_type in geometry_types
        ):
            planar_name, spherical_name, radius_power = area_function
            if not term.geography:
                return f"{planar_name}({','.join(arguments_sql)})"
            if spherical_name is None:
                raise UnSupportedError(f"ClickHouse computes the {function_type.value} of areas on a plane only")
            function_sql = f"{spherical_name}({','.join(arguments_sql)})"
            return cls.get_spherical_measure_sql(function_sql, radius_power) if radius_power else function_sql
        raise UnSupportedError(
            f"ClickHouse has no {function_type.value} of a {' and a '.join(item.value for item in geometry_types)}"
        )

    @staticmethod
    def render_spatial_aggregate(function: SpatialAggregateFunction, sql_context: SqlContext) -> str:
        """Refused - ClickHouse has no spatial aggregate.

        Raises:
            UnSupportedError: Always.
        """
        raise UnSupportedError(f"ClickHouse has no spatial aggregate - {type(function).__name__} has no SQL there")
