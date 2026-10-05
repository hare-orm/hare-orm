from __future__ import annotations

from typing import Any, ClassVar

from hare.gis.constants import GEOGRAPHY_REQUIRED_FEATURE
from hare.gis.enums import SpatialFunctionType
from hare.gis.fields.geometry_field import GeometryField
from hare.gis.spatial_features import SpatialFeatures
from hare.gis.terms.geometry_value import GeometryValue
from hare.gis.terms.spatial_function_term import SpatialFunctionTerm
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult, Function as HareFunction
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.terms.term import Term


class SpatialFunction(HareFunction):
    """Base of the spatial functions - the first argument is a geometry (a field name, ``F()`` or an
    expression), the next ``geometry_argument_count - 1`` arguments geometries too (``F()``, an
    expression or a geometry from Python, compared in the first one's SRID), then the function's own.
    Each dialect writes the SQL; one without spatial functions raises ``UnSupportedError``.
    """

    #: What the function computes.
    function_type: ClassVar[SpatialFunctionType]
    #: How many of the arguments are geometries.
    geometry_argument_count: ClassVar[int] = 1
    #: A geometry result is read through the first argument's field; another type through
    #: ``value_field``.
    populate_field_object = True

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("keeps_plan", PlanPartType.KEEPS_PLAN_METHOD),
        *HareFunction.plan_parts,
    )

    def keeps_plan(self) -> bool:
        """Whether the call keeps a plan - not with a geometry from Python, which decides the SQL
        around its parameter (a transform to the column's SRID)."""
        geometry_values = self.default_values[: self.geometry_argument_count - 1]
        return all(isinstance(value, (Expression, Term)) for value in geometry_values)

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        SpatialFeatures.raise_if_unsupported(expression_context, type(self).__name__)
        return super().get_result(expression_context)

    @staticmethod
    def get_geometry_field(function_arg: ExpressionResult) -> GeometryField | None:
        """The geometry field of the first argument, None when it isn't one.

        Args:
            function_arg: The resolved first argument.

        Returns:
            The field.
        """
        output_field = function_arg.output_field  # type: ignore[call-overload]
        return output_field if isinstance(output_field, GeometryField) else None

    def _get_default_terms(
        self,
        function_arg: ExpressionResult,
        default_results: list[ExpressionResult],
        expression_context: ExpressionContext,
    ) -> list[Any]:
        geometry_field = self.get_geometry_field(function_arg)
        if geometry_field is not None and geometry_field.geography:
            SpatialFeatures.raise_if_unsupported(expression_context, type(self).__name__, GEOGRAPHY_REQUIRED_FEATURE)
            if expression_context.connection is not None:
                SpatialFeatures.raise_if_unknown_reference_system(
                    expression_context.connection, type(self).__name__, geometry_field.srid
                )
        default_terms: list[Any] = []
        for index, (default_value, default_result) in enumerate(
            zip(self.default_values, default_results, strict=True)
        ):
            if index < self.geometry_argument_count - 1 and not isinstance(default_value, (Expression, Term)):
                default_terms.append(self.get_geometry_value(default_value, geometry_field))
            else:
                default_terms.append(default_result.term)
        return default_terms

    @staticmethod
    def get_geometry_value(value: Any, geometry_field: GeometryField | None) -> GeometryValue:
        """A geometry from Python as an argument - in the first argument's SRID.

        Args:
            value: The geometry, in any form a ``GeometryField`` takes.
            geometry_field: The first argument's field, None when it isn't a geometry field.

        Returns:
            The term.
        """
        field = geometry_field if geometry_field is not None else GeometryField()
        geometry = field.get_parsed_geometry(value)
        target_srid = geometry_field.srid if geometry_field is not None else geometry.srid
        geography = geometry_field.geography if geometry_field is not None else False
        return GeometryValue(geometry, target_srid, geography)  # type: ignore[arg-type]

    def _get_output_field(self, function_arg: ExpressionResult, default_results: list[ExpressionResult]) -> Any:
        # A measure, a flag or a text has its own field; a geometry is read as the first argument's.
        return self.value_field if self.value_field is not None else function_arg.output_field  # type: ignore[call-overload]

    def _get_function_term(self, function_arg: ExpressionResult, default_terms: list[Any]) -> Any:
        geometry_field = self.get_geometry_field(function_arg)
        geography = geometry_field.geography if geometry_field is not None else False
        return SpatialFunctionTerm(
            self.function_type,
            function_arg.term,
            *default_terms,
            geography=geography,
            geometry_argument_count=self.geometry_argument_count,
            geometry_fields=(geometry_field,),
        )
