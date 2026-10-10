from __future__ import annotations

#: What separates the names in a request's ordering, fields and include parameters
#: (``?ordering=-created_at,name``, ``?fields=id,title``, ``?include=author,tags``).
NAME_SEPARATOR = ","

#: The name ``Meta.filters`` may give the exact lookup, which has no suffix of its own - its
#: parameter is the field path itself.
EXACT_LOOKUP_NAME = "exact"

#: The default names of the parameters the options of a request query add.
DEFAULT_SEARCH_PARAMETER = "search"

DEFAULT_ORDERING_PARAMETER = "ordering"

DEFAULT_FIELDS_PARAMETER = "fields"

DEFAULT_INCLUDE_PARAMETER = "include"

DEFAULT_DELETED_PARAMETER = "deleted"

#: The fields of a ``VersionedModel`` naming a record and its version.
VERSIONED_RECORD_FIELD = "id"

OPTIMISTIC_LOCK_FIELD = "version"
