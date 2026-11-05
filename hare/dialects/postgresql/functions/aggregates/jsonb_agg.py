from __future__ import annotations

from typing import Any, ClassVar

from hare.dialects.postgresql.fields.interval_field import IntervalField
from hare.dialects.postgresql.functions.aggregates.jsonb_agg_field import JSONBAggField
from hare.exceptions import FieldError
from hare.fields.data.binary_field import BinaryField
from hare.fields.data.containers.array_field import ArrayField
from hare.fields.generated_field import GeneratedField
from hare.query.expressions import (
    Aggregate,
    ExpressionResult,
)


class JSONBAgg(Aggregate):
    """``JSONB_AGG(field)`` - a column's values of each group as a jsonb array; the values needn't
    share a type. Each element is decoded through the aggregated field.

    Example: ``Model.objects.annotate(children=JSONBAgg("child__name")).group_by("id")``
    """

    function_name = "JSONB_AGG"
    allows_order_by = True
    # Set so the query reads the selected output field - a JSONField, whatever the
    # aggregated field is.
    populate_field_object = True
    #: The fields JSON holds no value of - an interval and bytes become text of the server's own
    #: IntervalStyle/bytea_output, inside arrays too; ArrayAgg reads them as they are.
    refused_element_field_classes: ClassVar[tuple[type[Any], ...]] = (IntervalField, BinaryField)

    def _get_output_field(
        self, function_arg: ExpressionResult, default_results: list[ExpressionResult]
    ) -> JSONBAggField:
        """Decode JSON text and its elements through the aggregated field's reader."""
        source_field = function_arg.output_field
        effective_field = GeneratedField.get_effective_field(source_field) if source_field is not None else None
        while isinstance(effective_field, ArrayField):
            effective_field = GeneratedField.get_effective_field(effective_field.base_field)
        if isinstance(effective_field, self.refused_element_field_classes):
            raise FieldError(
                f"JSONBAgg() can't aggregate {type(effective_field).__name__} - JSON holds no such value; "
                "aggregate it with ArrayAgg()"
            )
        return JSONBAggField(source_field)
