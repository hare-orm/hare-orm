from __future__ import annotations

from typing import Any

from hare.dialects.postgresql.functions.aggregates.string_agg_function import StringAggFunction
from hare.fields.data.text.text_field import TextField
from hare.fields.field import Field
from hare.query.expressions import (
    Aggregate,
    ExpressionResult,
)


class StringAgg(Aggregate):
    """STRING_AGG(field::text, delimiter) - joins a column's values into one string per GROUP BY
    group. A non-text field is cast to text first, in Postgres's own text form of its type.

    Example: ``Model.objects.annotate(names=StringAgg("author__name", ", ")).group_by("id")``
    """

    database_function = StringAggFunction
    allows_order_by = True

    #: Shared, long-lived instance - the statement plans hold an annotation's output
    #: field weakly, so a fresh instance per call would be collected immediately.
    STRING_AGG_OUTPUT_FIELD = TextField()

    def _get_output_field(
        self, function_arg: ExpressionResult, default_results: list[ExpressionResult]
    ) -> Field[Any] | None:
        """The joined values are text whatever the aggregated field's type.

        Args:
            function_arg: The resolved main argument.
            default_results: The resolved delimiter.

        Returns:
            The shared text field.
        """
        return self.STRING_AGG_OUTPUT_FIELD  # type:ignore[call-overload]
