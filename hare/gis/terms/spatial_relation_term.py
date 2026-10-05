from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.gis.enums import SpatialRelation
from hare.sql.terms.functions.function import Function

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.gis.fields.geometry_field import GeometryField


class SpatialRelationTerm(Function):
    """Whether two geometries stand in a ``SpatialRelation`` - each dialect writes its own SQL.

    Args:
        relation: The relation.
        left: The column's geometry.
        right: The other geometry.
        extra_arguments: The relation's own arguments - ``relate``'s pattern, ``dwithin``'s distance.
        geography: Whether the column is a geography.
        field: The field of the column ``left`` is - a dialect finds the column's spatial index
            through it; None when ``left`` is no model's column.
    """

    requires_dialect_renderer = True

    def __init__(
        self,
        relation: SpatialRelation,
        left: Any,
        right: Any,
        *extra_arguments: Any,
        geography: bool,
        field: GeometryField | None = None,
    ) -> None:
        super().__init__("SPATIAL_RELATION", left, right, *extra_arguments)
        self.relation = relation
        self.geography = geography
        self.field = field
