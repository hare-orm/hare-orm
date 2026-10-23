from __future__ import annotations

#: The bounds of a 0-indexed array position - 32-bit, as a position is written one higher or counted
#: from the end.
ARRAY_ELEMENT_INDEX_MIN = -(2**31 - 1)
ARRAY_ELEMENT_INDEX_MAX = 2**31 - 2
