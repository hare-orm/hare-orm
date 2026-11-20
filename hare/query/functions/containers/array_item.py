from __future__ import annotations

from typing import Any, ClassVar

from hare.fields.data.containers.array_field import ArrayField
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField
from hare.query.expressions import (
    Expression,
    ExpressionContext,
    ExpressionResult,
    Function,
)
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.terms.containers import ArrayElementTerm
from hare.sql.terms.term import Term


class ArrayItem(Function):
    """One element of an ``ArrayField`` by 0-based position - a negative one counts from the end -,
    decoded through the array's ``base_field``; an element of a nested array is a whole row of it. To
    filter by an element use the ``__item=(index, value)`` lookup.

    Example: ``Model.objects.annotate(first=ArrayItem("values", 0))``

    Args:
        field: The array field - a name, ``F()`` or an expression.
        index: The 0-based position.

    Raises:
        ValidationError: ``index`` isn't an ``int`` or is out of range.
    """

    database_function = ArrayElementTerm
    populate_field_object = True
    plan_parts: ClassVar[DeclaredPlanParts] = (*Function.plan_parts, ("index", PlanPartType.NONE))

    def __init__(self, field: str | Expression | Term, index: int) -> None:
        super().__init__(field)
        self.index = ArrayElementTerm.get_validated_index(index)

    def get_plan_options(self) -> tuple[Any, ...]:
        return (self.index,)

    @staticmethod
    def _get_array_field(function_arg: ExpressionResult) -> ArrayField | None:
        """The indexed argument's own ``ArrayField``, when known.

        Args:
            function_arg: The resolved array argument.

        Returns:
            The array field, or None for an argument of unknown type.
        """
        output_field = function_arg.output_field
        if output_field is None:
            return None
        effective_field = GeneratedField.get_effective_field(output_field)
        return effective_field if isinstance(effective_field, ArrayField) else None

    def _get_default_terms(
        self,
        function_arg: ExpressionResult,
        default_results: list[ExpressionResult],
        expression_context: ExpressionContext,
    ) -> list[Any]:
        array_field = self._get_array_field(function_arg)
        element_field = array_field.base_field if array_field is not None else None
        return [self.index, element_field]

    def _get_output_field(
        self, function_arg: ExpressionResult, default_results: list[ExpressionResult]
    ) -> Field[Any] | None:
        array_field = self._get_array_field(function_arg)
        return array_field.base_field if array_field is not None else None
