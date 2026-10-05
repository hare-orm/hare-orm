from __future__ import annotations

#: Environment variable setting how many entries a bucket of a statement plan cache (``Cache``) keeps.
ENV_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL = "HARE_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL"

#: The default: enough for one model's variety of queries, while bounding the growth from `__in=`
#: lists of ever different lengths.
DEFAULT_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL = 512

#: The bounds of the configured bucket size - a sanity check against a typo, not a tuning limit.
MIN_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL = 1

MAX_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL = 1_000_000
