from __future__ import annotations

import re

from hare.lazy_loading.lazy_pattern import LazyPattern

#: The volatile functions a database default can call - PostgreSQL evaluates such a default for
#: every existing row, rewriting the table, when a column with it is added.
POSTGRES_VOLATILE_DEFAULT_FUNCTIONS_RE = LazyPattern(
    r"\b(?:random|gen_random_uuid|uuid_generate_v1|uuid_generate_v1mc|uuid_generate_v4|uuidv4|uuidv7|"
    r"clock_timestamp|timeofday|nextval|txid_current|pg_current_xact_id)\s*\(",
    re.IGNORECASE,
)

#: The planner's row estimate of a table, -1 when it was never analyzed; no row for a missing table.
POSTGRES_TABLE_ROW_ESTIMATE_SQL = (
    "SELECT c.reltuples::bigint AS estimate FROM pg_class c WHERE c.oid = to_regclass({table})"
)
