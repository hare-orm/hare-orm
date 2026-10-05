from __future__ import annotations

from typing import Any

from hare.fields.data.numeric.int_field import IntField
from hare.fields.field import Field
from hare.query.expressions import Function
from hare.query.expressions.expression_result import ExpressionResult


class Length(Function):
    """Returns length of text/blob, e.g. ``Length("field_name")``."""

    function_name = "LENGTH"

    #: Shared, long-lived instance - the statement plans hold an annotation's output
    #: field weakly, so a fresh instance per call would be collected immediately.
    LENGTH_OUTPUT_FIELD = IntField()

    value_field = LENGTH_OUTPUT_FIELD

    def _get_output_field(
        self, function_arg: ExpressionResult, default_results: list[ExpressionResult]
    ) -> Field[Any] | None:
        """A length is an integer whatever its argument's type - so a filter on a ``Length()``
        annotation compares and binds integers.

        Args:
            function_arg: The resolved main argument.
            default_results: The resolved extra arguments.

        Returns:
            The shared integer field.
        """
        return self.LENGTH_OUTPUT_FIELD  # type:ignore[call-overload]
