from __future__ import annotations

from typing import Any

from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.data.numeric.float_field import FloatField
from hare.fields.data.numeric.int_field import IntField
from hare.fields.field import Field

#: The largest offset of a window frame edge - a 64-bit integer, the widest a database takes.
MAX_WINDOW_FRAME_OFFSET = 2**63 - 1

#: The fields a frame of value offsets orders by - the offset is added to and taken from a number.
VALUE_OFFSET_ORDERING_FIELD_CLASSES: tuple[type[Field[Any]], ...] = (IntField, FloatField, DecimalField)
