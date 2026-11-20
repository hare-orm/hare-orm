from __future__ import annotations

from typing import Any

from hare.gis.enums import SpatialRelation
from hare.gis.functions.constants import FLAG_FIELD
from hare.gis.functions.spatial_function import SpatialFunction
from hare.gis.spatial_distances import SpatialDistances
from hare.gis.terms.spatial_relation_term import SpatialRelationTerm
from hare.query.expressions import Expression, ExpressionResult


class DWithin(SpatialFunction):
    """Whether two geometries are within a radius, using the dialect's spatial predicate.

    Args:
        expression: A geometry field name or expression.
        other: A Python geometry or another geometry expression.
        distance: A finite nonnegative radius or an expression computing one. Units are metres
            for geography and the SRID's units for geometry.

    The result is boolean and can be annotated or used in a condition. Rendering is shared
    with __dwithin, including the database's spatial-index support and capability checks.
    """

    geometry_argument_count = 2
    default_value_counts = (2, 2)
    value_field = FLAG_FIELD

    def __init__(self, expression: Any, other: Any, distance: Any) -> None:
        for literal in Expression.get_literals_of(distance):
            SpatialDistances.get_value(literal, "DWithin")
        super().__init__(expression, other, distance)

    def _get_function_term(self, function_arg: ExpressionResult, default_terms: list[Any]) -> SpatialRelationTerm:
        field = self.get_geometry_field(function_arg)
        return SpatialRelationTerm(
            SpatialRelation.DWITHIN,
            function_arg.term,
            *default_terms,
            geography=field.geography if field is not None else False,
            field=field,
        )
