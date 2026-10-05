from __future__ import annotations

#: How many rows of values of several fields a relation check looks for at a time - the values of one field
#: go in one query, as many as the database binds.
CONSTRAINT_CHECK_CHUNK_SIZE = 1000
#: The separator of the parts of a filter's key, and the suffix of an equality written out.
LOOKUP_SEPARATOR = "__"
EXACT_LOOKUP_SUFFIX = "__exact"
