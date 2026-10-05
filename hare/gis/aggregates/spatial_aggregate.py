from __future__ import annotations

from typing import Any, ClassVar

from hare.exceptions import UnSupportedError
from hare.fields.field import Field
from hare.gis.enums import SpatialFunctionType
from hare.gis.fields.geometry_field import GeometryField
from hare.gis.spatial_features import SpatialFeatures
from hare.gis.terms.spatial_aggregate_function import SpatialAggregateFunction
from hare.query.expressions import Aggregate, ExpressionContext, ExpressionResult


class SpatialAggregate(Aggregate):
    """Base of the spatial aggregates - each merges the geometries of a group (``distinct=``,
    ``_filter=`` as any aggregate). Geometry columns only: a geography has no spatial aggregates.
    """

    database_function = SpatialAggregateFunction
    populate_field_object = True

    #: What the aggregate computes.
    function_type: ClassVar[SpatialFunctionType]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # The term's name is the type's value - each dialect renames it.
        if "function_type" in cls.__dict__:
            cls.function_name = cls.function_type.value

    def _wrap_argument(
        self, expression_context: ExpressionContext, function_arg: ExpressionResult
    ) -> ExpressionResult:
        SpatialFeatures.raise_if_unsupported(expression_context, type(self).__name__)
        connection = expression_context.connection
        if self.order_by and connection is not None and not connection.features.supports_ordered_aggregates:
            raise UnSupportedError(
                f"{type(self).__name__}(order_by=...) can't run on the {connection.connection_alias!r} connection: "
                "it needs features.supports_ordered_aggregates (SQLite 3.44+)"
            )
        argument_field = function_arg.output_field  # type: ignore[call-overload]
        if isinstance(argument_field, GeometryField) and argument_field.geography:
            raise UnSupportedError(f"{type(self).__name__} aggregates geometry columns, not a geography")
        return super()._wrap_argument(expression_context, function_arg)

    def _coerce_output_field(self, field_object: Field[Any]) -> Field[Any]:
        return self.value_field if self.value_field is not None else field_object
