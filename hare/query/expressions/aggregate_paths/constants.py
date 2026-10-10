from __future__ import annotations

#: Separator between a to-many relation path and the filter-call generation of the separate JOIN a
#: later `.filter()`/`.exclude()` call builds over that relation, in the paths the aggregate fan-out
#: check tracks - `tags#2` is a second JOIN over `tags`, distinct from the `tags` JOIN itself.
SEPARATE_FILTER_JOIN_PATH_SEPARATOR = "#"

#: Lookup suffix of a filter that, with the value `True`, keeps only the rows a JOIN found no
#: related row for.
ISNULL_LOOKUP_SUFFIX = "isnull"

#: Filter value types holding several values - an equality with one of them never narrows a
#: to-many JOIN to one related row.
MULTIPLE_VALUE_TYPES: tuple[type, ...] = (list, tuple, set, frozenset, dict)
