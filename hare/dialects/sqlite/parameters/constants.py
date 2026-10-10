from __future__ import annotations

#: The list length from which `__in`/`__not_in` binds the list as one JSON array parameter (`IN
#: (SELECT value FROM json_each(?))`) - one statement plan for every longer list, and no
#: bind-parameter ceiling.
SQLITE_IN_JSON_ARRAY_THRESHOLD = 20
