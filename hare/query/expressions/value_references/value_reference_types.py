from __future__ import annotations

from typing import Any

from hare.query.expressions.value_references.array_value_reference import ArrayValueReference
from hare.query.expressions.value_references.composite_key_value_reference import CompositeKeyValueReference
from hare.query.expressions.value_references.cursor_value_reference import CursorValueReference
from hare.query.expressions.value_references.encoded_value_reference import EncodedValueReference
from hare.query.expressions.value_references.like_value_reference import LikeValueReference
from hare.query.expressions.value_references.list_parameter_value_reference import ListParameterValueReference
from hare.query.expressions.value_references.list_value_reference import ListValueReference
from hare.query.expressions.value_references.literal_value_reference import LiteralValueReference
from hare.query.expressions.value_references.open_range_value_reference import OpenRangeValueReference
from hare.query.expressions.value_references.range_value_reference import RangeValueReference
from hare.query.expressions.value_references.rebuilt_criterion_value_reference import RebuiltCriterionValueReference
from hare.query.expressions.value_references.related_key_value_reference import RelatedKeyValueReference
from hare.query.expressions.value_references.related_value_reference import RelatedValueReference
from hare.query.expressions.value_references.row_list_value_reference import RowListValueReference
from hare.query.expressions.value_references.scalar_value_reference import ScalarValueReference
from hare.query.expressions.value_references.write_value_reference import WriteValueReference

#: The parameters one value of a query gives: per parameter the ``id()`` of the term it stands for
#: in the built query, the bound value, and whether the term always renders as a parameter
#: (``ParameterizedValueWrapper``) rather than only when the value is one a parameter can carry.
type ParameterValues = list[tuple[int, Any, bool]]


#: A reference to the value a filter compares with.
type FilterValueReference = (
    ScalarValueReference
    | ListValueReference
    | ListParameterValueReference
    | RangeValueReference
    | OpenRangeValueReference
    | LikeValueReference
    | ArrayValueReference
    | EncodedValueReference
)


#: Any reference to one value of a built query - what a query running on a plan binds its value
#: through.
type ValueReference = (
    ScalarValueReference
    | WriteValueReference
    | ListValueReference
    | ListParameterValueReference
    | RowListValueReference
    | RelatedKeyValueReference
    | RangeValueReference
    | OpenRangeValueReference
    | RebuiltCriterionValueReference
    | CompositeKeyValueReference
    | LiteralValueReference
    | CursorValueReference
    | RelatedValueReference
    | LikeValueReference
    | ArrayValueReference
    | EncodedValueReference
)


#: The value references a query records while it is built: each with the origin of its value
#: (``PlanOrigins``) - None for a value no reference can rebind.
type RecordedValueReferences = list[tuple[Any, ValueReference | None]]
