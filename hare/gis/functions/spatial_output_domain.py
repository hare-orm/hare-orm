from __future__ import annotations

from dataclasses import dataclass

from hare.gis.constants import DEFAULT_SRID, GEOMETRY_DIMENSIONS
from hare.gis.enums import GeometryType, SpatialFunctionType
from hare.gis.fields.declarations import PointField
from hare.gis.fields.geometry_field import GeometryField


@dataclass(frozen=True, slots=True)
class SpatialOutputDomain:
    """The geometry returned by an expression, independent of its input column declaration.

    Variable-shape or variable-dimension functions retain that domain instead of claiming
    the input column's constraints. A dialect refines the native function's result before
    its reader and lookup field are selected.
    """

    shape: GeometryType
    dimensions: frozenset[int]
    srid: int | None
    geography: bool

    @classmethod
    def get_default(
        cls, operation: SpatialFunctionType, source: GeometryField, target_srid: int
    ) -> SpatialOutputDomain:
        """Describe the portable result; native renderers refine storage and dimension rules."""
        shape = GeometryType.GEOMETRY
        dimensions = GEOMETRY_DIMENSIONS
        srid = source.srid
        if operation in (SpatialFunctionType.CENTROID, SpatialFunctionType.POINT_ON_SURFACE):
            shape = GeometryType.POINT
        if operation == SpatialFunctionType.BUFFER:
            dimensions = frozenset((2,))
        if operation in (SpatialFunctionType.TRANSFORM, SpatialFunctionType.SIMPLIFY):
            shape = source.get_geometry_type()
            dimensions = source.get_geometry_dimensions()
        if operation == SpatialFunctionType.TRANSFORM:
            srid = target_srid
        return cls(shape, dimensions, srid, False)

    def get_field(self, source: GeometryField) -> GeometryField:
        """Preserve a custom reader only when the function stays within its value domain."""
        if (
            self.shape == source.get_geometry_type()
            and self.dimensions == source.get_geometry_dimensions()
            and self.geography == source.geography
        ):
            field = source.snapshot()
        else:
            field_class = PointField if self.shape == GeometryType.POINT else GeometryField
            field = field_class(
                self.shape,
                srid=DEFAULT_SRID if self.srid is None else self.srid,
                geography=self.geography,
                dimensions=min(self.dimensions),
            )
        field.srid = self.srid  # type: ignore[assignment]
        field._result_dimensions = self.dimensions
        return field
