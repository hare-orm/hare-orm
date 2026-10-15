from __future__ import annotations

from hare.classes.declared_subclass import DeclaredSubclass
from hare.gis.aggregates.spatial_aggregate import SpatialAggregate
from hare.gis.enums import SpatialFunctionType
from hare.gis.fields.extent_field import ExtentField

Collect = DeclaredSubclass.make(
    SpatialAggregate,
    "Collect",
    __package__,
    """The geometries of a group as one multi geometry or collection, without merging them.""",
    function_type=SpatialFunctionType.COLLECT,
)

UnionAggregate = DeclaredSubclass.make(
    SpatialAggregate,
    "UnionAggregate",
    __package__,
    """The geometries of a group merged into one - overlapping areas dissolved.""",
    function_type=SpatialFunctionType.UNION_AGGREGATE,
)

MakeLine = DeclaredSubclass.make(
    SpatialAggregate,
    "MakeLine",
    __package__,
    """The points of a group as one line, in the order of the aggregate's ``order_by=``.""",
    function_type=SpatialFunctionType.MAKE_LINE,
    allows_order_by=True,
)

Extent = DeclaredSubclass.make(
    SpatialAggregate,
    "Extent",
    __package__,
    """The bounding box of a group's geometries as ``(min x, min y, max x, max y)``.""",
    function_type=SpatialFunctionType.EXTENT,
    value_field=ExtentField(),
)
