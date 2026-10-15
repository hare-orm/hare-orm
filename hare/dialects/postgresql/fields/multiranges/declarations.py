from __future__ import annotations

from hare.classes.declared_subclass import DeclaredSubclass
from hare.dialects.postgresql.fields.multiranges.multi_range_field import MultiRangeField
from hare.dialects.postgresql.fields.ranges.date_range_field import DateRangeField
from hare.dialects.postgresql.fields.ranges.date_time_range_field import DateTimeRangeField
from hare.dialects.postgresql.fields.ranges.decimal_range_field import DecimalRangeField
from hare.dialects.postgresql.fields.ranges.declarations import BigIntRangeField
from hare.dialects.postgresql.fields.ranges.int_range_field import IntRangeField

IntMultiRangeField = DeclaredSubclass.make(
    MultiRangeField,
    "IntMultiRangeField",
    __package__,
    """``int4multirange`` - non-overlapping ranges of 32-bit integers.""",
    SQL_TYPE="int4multirange",
    RANGE_FIELD_CLASS=IntRangeField,
)

BigIntMultiRangeField = DeclaredSubclass.make(
    MultiRangeField,
    "BigIntMultiRangeField",
    __package__,
    """``int8multirange`` - non-overlapping ranges of 64-bit integers.""",
    SQL_TYPE="int8multirange",
    RANGE_FIELD_CLASS=BigIntRangeField,
)

DecimalMultiRangeField = DeclaredSubclass.make(
    MultiRangeField,
    "DecimalMultiRangeField",
    __package__,
    """``nummultirange`` - non-overlapping ranges of arbitrary-precision decimals.""",
    SQL_TYPE="nummultirange",
    RANGE_FIELD_CLASS=DecimalRangeField,
)

DateMultiRangeField = DeclaredSubclass.make(
    MultiRangeField,
    "DateMultiRangeField",
    __package__,
    """``datemultirange`` - non-overlapping ranges of dates.""",
    SQL_TYPE="datemultirange",
    RANGE_FIELD_CLASS=DateRangeField,
)

DateTimeMultiRangeField = DeclaredSubclass.make(
    MultiRangeField,
    "DateTimeMultiRangeField",
    __package__,
    """``tstzmultirange`` - non-overlapping ranges of timezone-aware timestamps, bounds read like
    ``DateTimeRangeField``'s.""",
    SQL_TYPE="tstzmultirange",
    RANGE_FIELD_CLASS=DateTimeRangeField,
)
