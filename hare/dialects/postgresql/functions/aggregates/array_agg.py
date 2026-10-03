from typing import Any

from hare.dialects.postgresql.fields.array import ArrayField
from hare.fields.base.field import Field
from hare.query.expressions import (
    Aggregate,
)


class ArrayAgg(Aggregate):
    """``ARRAY_AGG(field)`` - a column's values of each group as an array, each element decoded through
    the aggregated field.

    Example: ``Model.objects.annotate(tags=ArrayAgg("tag__name", distinct=True)).group_by("id")``
    """

    function_name = "ARRAY_AGG"
    populate_field_object = True
    allows_order_by = True

    def _coerce_output_field(self, field_object: Field[Any]) -> Field[Any]:
        return ArrayField(base_field=field_object)
