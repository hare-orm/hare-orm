from __future__ import annotations

#: The directory the stubs go to by default - pyright's ``stubPath`` by default.
DEFAULT_STUBS_DIRECTORY = "typings"
#: How many relations a filter key of a stub crosses at most by default.
DEFAULT_RELATION_DEPTH = 2
#: The most relations a filter key of a stub may cross.
MAX_RELATION_DEPTH = 5
#: The names a stub gives a model's declarations, after the model's own name.
FILTERS_SUFFIX = "Filters"
WRITES_SUFFIX = "Writes"
QUERYSET_SUFFIX = "QuerySet"
#: The first line of every stub ``hare stubs`` writes.
STUB_HEADER = "# Written by `hare stubs` - run it again after changing the models."
