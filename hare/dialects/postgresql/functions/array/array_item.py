from typing import Any

from hare.dialects.postgresql.fields.array import ArrayField
from hare.dialects.postgresql.functions.array.array_subscript import ArraySubscript
from hare.fields.base.field import Field
from hare.query.expressions import (
    ExpressionContext,
    ExpressionResult,
    F,
    Function as HareFunction,
)
from hare.sql.terms.base.term import Term


class ArrayItem(HareFunction):
    """One element of an ``ArrayField`` by 0-based position, decoded through the array's
    ``base_field``; an element of a nested array is a whole row of it. To filter by an element use
    the ``__item=(index, value)`` lookup.

    Example: ``Model.objects.annotate(first=ArrayItem("values", 0))``

    Args:
        field: The array field - a name, ``F()`` or an expression.
        index: The 0-based position.

    Raises:
        ValidationError: ``index`` isn't an ``int`` or is out of range.
    """

    database_func = ArraySubscript
    populate_field_object = True

    def __init__(self, field: str | F | HareFunction | Term, index: int) -> None:
        super().__init__(field, ArraySubscript.get_validated_index(index))

    def _get_array_field(self, function_arg: ExpressionResult) -> ArrayField | None:
        """The indexed argument's own ``ArrayField``, when known.

        Args:
            function_arg: The resolved array argument.

        Returns:
            The array field, or None for an argument of unknown type.
        """
        output_field = function_arg.output_field  # type:ignore[call-overload]
        if output_field is None:
            return None
        effective_field = self._get_effective_field_object(output_field)
        return effective_field if isinstance(effective_field, ArrayField) else None

    def _get_default_terms(
        self,
        function_arg: ExpressionResult,
        default_results: list[ExpressionResult],
        expression_context: ExpressionContext,
    ) -> list[Any]:
        array_field = self._get_array_field(function_arg)
        subarray_field = None
        if array_field is not None and isinstance(array_field.base_field, ArrayField):
            subarray_field = array_field.base_field
        return [*super()._get_default_terms(function_arg, default_results, expression_context), subarray_field]

    def _get_output_field(
        self, function_arg: ExpressionResult, default_results: list[ExpressionResult]
    ) -> Field[Any] | None:
        array_field = self._get_array_field(function_arg)
        return array_field.base_field if array_field is not None else None
