from __future__ import annotations

from typing import Any

from hare.dialects.postgresql.fields.multiranges.multi_range_field import MultiRangeField
from hare.fields.field import Field
from hare.query.expressions import (
    Aggregate,
)


class RangeAgg(Aggregate):
    """``RANGE_AGG(field)`` - the union of a range column's values of each group, as a multirange.

    Example: ``Room.objects.values("building").annotate(busy=RangeAgg("booking__during"))``
    """

    function_name = "RANGE_AGG"
    populate_field_object = True

    def _coerce_output_field(self, field_object: Field[Any]) -> Field[Any]:
        return MultiRangeField.get_field_for_range_field(field_object)
