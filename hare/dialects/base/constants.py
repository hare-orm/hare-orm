from __future__ import annotations

from hare.dialects.base.sql_dialect import SqlDialect

#: How many rows are read or written before yielding to the event loop.
CHUNK_SIZE = 2000


#: How many leading characters of the table name / first field name survive truncation in a
#: generated index name, before the digest suffix (INDEX_NAME_HASH_LENGTH).
INDEX_NAME_TABLE_PREFIX_LENGTH = 11
INDEX_NAME_FIELD_PREFIX_LENGTH = 7
#: 48 bits of hash keep generated index names of similarly named tables from colliding; the longest
#: name (37 chars) fits a 63-byte identifier limit.
INDEX_NAME_HASH_LENGTH = 12

TRUE_STRINGS = frozenset({"true", "1", "yes", "on"})
FALSE_STRINGS = frozenset({"false", "0", "no", "off"})


#: A query-executing call taking at least this long gets one extra DEBUG-level log line - a
#: generous default meant to flag genuinely slow queries, not every normal round-trip.
SLOW_QUERY_THRESHOLD_MS = 500.0


#: The dialect of plain SQL rendered for no particular database.
SQL_DIALECT = SqlDialect()

#: Bind parameters left unused by prefetch_related()'s batched through-table ``IN`` lookups, out
#: of the backend's own per-statement ceiling - room for the through model's own tenant/soft-delete
#: filter.
PREFETCH_BIND_PARAMETERS_HEADROOM = 100

#: Most composite-key rows one prefetch_related() query filters on - each row becomes an AND-group
#: of a flat OR chain, and a chain that long can hit a backend's own expression-depth limit
#: (``Features.max_expression_depth``) or Python's recursion limit well before the bind-parameter ceiling.
PREFETCH_MAX_COMPOSITE_ROWS = 500
#: The parents of a sliced prefetch whose composite keys one query reads - their OR-ed equality groups
#: sit under the slice's window and subquery, which a database's expression-depth limit
#: (``Features.max_expression_depth``) bounds.
PREFETCH_MAX_SLICED_COMPOSITE_PARENTS = 200
