from __future__ import annotations

from typing import Any

from hare.fields.data.binary_field import BinaryField
from hare.fields.data.numeric.int_field import IntField
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField
from hare.query.expressions import Function
from hare.query.expressions.compatibility.constants import NON_TEXT_VALUE_TYPES
from hare.query.expressions.expression_result import ExpressionResult
from hare.sql.functions.length import Length as LengthTerm


class Length(Function):
    """The length of a text in characters, of bytes in bytes - an integer: ``Length("name")``."""

    function_name = "LENGTH"
    refused_argument_value_types = NON_TEXT_VALUE_TYPES
    default_value_counts = (0, 0)

    #: Shared result descriptors avoid allocating a field for each expression build.
    LENGTH_OUTPUT_FIELD = IntField()

    value_field = LENGTH_OUTPUT_FIELD

    def _get_function_term(self, function_arg: ExpressionResult, default_terms: list[Any]) -> LengthTerm:
        field = function_arg.output_field
        counts_bytes = field is not None and isinstance(GeneratedField.get_effective_field(field), BinaryField)
        return LengthTerm(function_arg.term, counts_bytes=counts_bytes)

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
        return self.LENGTH_OUTPUT_FIELD
