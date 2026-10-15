from __future__ import annotations

from hare.dialects.postgresql.functions.aggregates.jsonb_agg_field import JSONBAggField
from hare.fields.data.json.json_field import JSONField
from hare.fields.generated_field import GeneratedField
from hare.query.expressions import (
    Aggregate,
    ExpressionContext,
    ExpressionResult,
)


class JSONBAgg(Aggregate):
    """``JSONB_AGG(field)`` - a column's values of each group as a jsonb array; the values needn't
    share a type. Each element is decoded through the aggregated field.

    Example: ``Model.objects.annotate(children=JSONBAgg("child__name")).group_by("id")``
    """

    function_name = "JSONB_AGG"
    allows_order_by = True
    # Set so the query reads the output field get_result() gives - a JSONField, whatever the
    # aggregated field is.
    populate_field_object = True

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        # asyncpg returns jsonb columns as raw text, not a parsed value, unlike its native array
        # codec (which is why ArrayAgg needs no such override).
        result = super().get_result(expression_context)
        # A new result carrying the field - the expression instance may be shared by queries.
        source_field = result.output_field  # type:ignore[call-overload]
        element_field = None
        if source_field is not None and not isinstance(GeneratedField.get_effective_field(source_field), JSONField):
            element_field = source_field
        return ExpressionResult(term=result.term, joins=result.joins, output_field=JSONBAggField(element_field))
