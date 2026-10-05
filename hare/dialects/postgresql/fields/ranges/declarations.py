from __future__ import annotations

from hare.classes.declared_subclass import DeclaredSubclass
from hare.dialects.postgresql.fields.ranges.int_range_field import IntRangeField

BigIntRangeField = DeclaredSubclass.make(
    IntRangeField,
    "BigIntRangeField",
    __package__,
    """``int8range`` - a range of 64-bit integers.""",
    SQL_TYPE="int8range",
    ELEMENT_SQL_TYPE="bigint",
)
