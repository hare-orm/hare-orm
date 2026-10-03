from __future__ import annotations

from typing import Any

from hare.query.expressions.value_refs.array_value_ref import ArrayValueRef
from hare.query.expressions.value_refs.cursor_value_ref import CursorValueRef
from hare.query.expressions.value_refs.encoded_value_ref import EncodedValueRef
from hare.query.expressions.value_refs.like_value_ref import LikeValueRef
from hare.query.expressions.value_refs.list_parameter_value_ref import ListParameterValueRef
from hare.query.expressions.value_refs.list_value_ref import ListValueRef
from hare.query.expressions.value_refs.literal_value_ref import LiteralValueRef
from hare.query.expressions.value_refs.range_value_ref import RangeValueRef
from hare.query.expressions.value_refs.related_key_value_ref import RelatedKeyValueRef
from hare.query.expressions.value_refs.related_value_ref import RelatedValueRef
from hare.query.expressions.value_refs.row_list_value_ref import RowListValueRef
from hare.query.expressions.value_refs.scalar_value_ref import ScalarValueRef
from hare.query.expressions.value_refs.write_value_ref import WriteValueRef

#: The parameters one value of a query gives: per parameter the ``id()`` of the term it stands for
#: in the built query, the bound value, and whether the term always renders as a parameter
#: (``ParameterizedValueWrapper``) rather than only when the value is one a parameter can carry.
type ParameterValues = list[tuple[int, Any, bool]]


#: Any reference to one value of a built query - what a query running on a plan binds its value
#: through.
type ValueRef = (
    ScalarValueRef
    | WriteValueRef
    | ListValueRef
    | ListParameterValueRef
    | RowListValueRef
    | RelatedKeyValueRef
    | RangeValueRef
    | LiteralValueRef
    | CursorValueRef
    | RelatedValueRef
    | LikeValueRef
    | ArrayValueRef
    | EncodedValueRef
)


#: The value references a query records while it is built, in resolution order: each value's key
#: and its reference, or None for a value no reference can rebind.
type RecordedValueRefs = list[tuple[str, ValueRef | None]]
