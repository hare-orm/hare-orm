from __future__ import annotations

from hare.sql.terms.field import Field
from hare.sql.terms.functions.function import Function
from hare.sql.terms.tuple import Tuple

#: Criterion classes that are operands (a column, a function call, a value list) rather than
#: conditions - rewritten as a whole into a column of the derived table a window filter reads.
WINDOW_FILTER_OPERAND_CRITERION_TYPES: tuple[type, ...] = (Field, Function, Tuple)
