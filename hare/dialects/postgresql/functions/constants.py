from __future__ import annotations

#: Matches the text form of a one-row slice (``{{1,2}}``) of a multidimensional array, capturing
#: it without its outermost braces (``{1,2}``) - the text form of that row as an array of its own.
ARRAY_OUTER_BRACES_PATTERN = r"^\{(.*)\}$"

#: ``regexp_replace()`` replacement keeping ``ARRAY_OUTER_BRACES_PATTERN``'s captured row text.
ARRAY_OUTER_BRACES_REPLACEMENT = r"\1"
