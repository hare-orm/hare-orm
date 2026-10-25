from __future__ import annotations

#: The list length from which `__in`/`__not_in` binds the list as one JSON array parameter (`IN
#: (SELECT value FROM json_each(?))`) - one statement plan for every longer list, and no
#: bind-parameter ceiling.
SQLITE_IN_JSON_ARRAY_THRESHOLD = 20

#: A bound Decimal is written in fixed-point notation (``0.0000000001``) only while its exponent
#: stays within this many places either side of the point - past that, fixed-point text would grow
#: without bound and Decimal's own scientific text is kept.
SQLITE_DECIMAL_FIXED_POINT_MAX_EXPONENT = 1000
