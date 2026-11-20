from __future__ import annotations

from dataclasses import replace
from functools import partial
from typing import Any, ClassVar

from hare.fields.generated_field import GeneratedField
from hare.gis.constants import GEOGRAPHY_REQUIRED_FEATURE, SPATIAL_LOOKUP_REQUIRED_FEATURE
from hare.gis.enums import SpatialFunctionType
from hare.gis.fields.geometry_field import GeometryField
from hare.gis.functions.spatial_output_domain import SpatialOutputDomain
from hare.gis.geometries.geometry import Geometry
from hare.gis.geometry_arguments import GeometryArguments
from hare.gis.spatial_features import SpatialFeatures
from hare.gis.terms.spatial_function_term import SpatialFunctionTerm
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult, Function as HareFunction
from hare.query.expressions.structure.enums import ArgumentRole
from hare.query.expressions.structure.expression_structure import ExpressionStructure
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.terms.term import Term


class SpatialFunction(HareFunction):
    """Base of the spatial functions - the first argument is a geometry (a field name, ``F()`` or an
    expression), the next ``geometry_argument_count - 1`` arguments geometries too (``F()``, an
    expression or a geometry from Python, compared in the first one's SRID), then the function's own.
    Each dialect writes the SQL; one without spatial functions raises ``UnSupportedError``.
    """

    structure = ExpressionStructure(
        positions=((0, ArgumentRole.FIELD),),
        keywords=(("field", ArgumentRole.FIELD), ("expression", ArgumentRole.FIELD)),
    )

    #: What the function computes.
    function_type: ClassVar[SpatialFunctionType]
    #: How many of the arguments are geometries.
    geometry_argument_count: ClassVar[int] = 1
    default_value_counts = (0, 0)
    #: A geometry result is read through the first argument's field; another type through
    #: ``value_field``.
    populate_field_object = True

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("keeps_plan", PlanPartType.KEEPS_PLAN_METHOD),
        ("get_geometry_plan_key", PlanPartType.KEY_METHOD),
        *HareFunction.plan_parts,
    )

    def keeps_plan(self) -> bool:
        """Keep geometries with explicit reference-system metadata and expression arguments."""
        geometry_values = self.default_values[: self.geometry_argument_count - 1]
        return all(isinstance(value, (Expression, Term, Geometry)) for value in geometry_values)

    def get_geometry_plan_key(self) -> tuple[Any, ...]:
        """Include metadata controlling casts/transforms, independently of bound coordinates."""
        return tuple(
            (value.GEOMETRY_TYPE, value.srid, value.has_z) if isinstance(value, Geometry) else None
            for value in self.default_values[: self.geometry_argument_count - 1]
        )

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        expression_context.require_connection_feature(type(self).__name__, SPATIAL_LOOKUP_REQUIRED_FEATURE)
        return super().get_result(expression_context)

    @staticmethod
    def get_geometry_field(function_arg: ExpressionResult) -> GeometryField | None:
        """The geometry field of the first argument, None when it isn't one.

        Args:
            function_arg: The resolved first argument.

        Returns:
            The field.
        """
        output_field = function_arg.output_field
        if output_field is not None:
            output_field = GeneratedField.get_effective_field(output_field)
        return output_field if isinstance(output_field, GeometryField) else None

    def _get_default_terms(
        self,
        function_arg: ExpressionResult,
        default_results: list[ExpressionResult],
        expression_context: ExpressionContext,
    ) -> list[Any]:
        geometry_field = self.get_geometry_field(function_arg)
        if geometry_field is not None and geometry_field.geography:
            expression_context.require_connection_feature(type(self).__name__, GEOGRAPHY_REQUIRED_FEATURE)
            if expression_context.connection is not None:
                SpatialFeatures.raise_if_unknown_reference_system(
                    expression_context.connection, type(self).__name__, geometry_field.srid
                )
        default_terms: list[Any] = []
        for index, (default_value, default_result) in enumerate(
            zip(self.default_values, default_results, strict=True)
        ):
            if index < self.geometry_argument_count - 1 and not isinstance(default_value, (Expression, Term)):
                geometry_term = GeometryArguments.get_value(
                    default_value, geometry_field, dialect=expression_context.dialect
                )
                default_terms.append(geometry_term)
                if expression_context.value_wrapper_references is not None:
                    ExpressionArguments.record_literal(
                        expression_context.value_wrapper_references,
                        self,
                        "default_values",
                        geometry_term.args[0],
                        partial(
                            GeometryArguments.get_parameter_value,
                            field=geometry_field,
                            dialect=expression_context.dialect,
                        ),
                        index,
                    )
            else:
                default_terms.append(default_result.term)
        return default_terms

    def _get_output_field(self, function_arg: ExpressionResult, default_results: list[ExpressionResult]) -> Any:
        # A measure, a flag or a text has its own field; a geometry is read as the first argument's.
        return self.value_field if self.value_field is not None else function_arg.output_field

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

    def _get_result_term(
        self,
        term: Term,
        function_arg: ExpressionResult,
        default_results: list[ExpressionResult],
        expression_context: ExpressionContext,
    ) -> Term:
        """Carry the metadata of every geometry operand to dialect rendering."""
        if isinstance(term, SpatialFunctionTerm):
            term.geometry_fields = tuple(
                self.get_geometry_field(result)
                for result in (function_arg, *default_results[: self.geometry_argument_count - 1])
            )
        return term

    def _finalize_result(
        self, result: ExpressionResult, function_arg: ExpressionResult, expression_context: ExpressionContext
    ) -> ExpressionResult:
        """Choose readers and lookup conversion from the function's actual result domain."""
        source = self.get_geometry_field(function_arg)
        if source is None or self.value_field is not None or not isinstance(result.term, SpatialFunctionTerm):
            return result
        domain = SpatialOutputDomain.get_default(
            self.function_type,
            source,
            self.default_values[0] if self.function_type == SpatialFunctionType.TRANSFORM else source.srid,
        )
        domain = expression_context.dialect.renderers.get_spatial_output_domain(result.term, domain)
        return replace(result, output_field=domain.get_field(source))
