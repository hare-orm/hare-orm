from __future__ import annotations

from hare.fields.narrowing.decimal_digits_limit import DecimalDigitsLimit
from hare.fields.narrowing.enum_values_limit import EnumValuesLimit
from hare.fields.narrowing.integer_range_limit import IntegerRangeLimit
from hare.fields.narrowing.text_length_limit import TextLengthLimit

NarrowingLimit = TextLengthLimit | IntegerRangeLimit | DecimalDigitsLimit | EnumValuesLimit
