from __future__ import annotations

#: The page size a pagination returns when the request names none.
DEFAULT_PAGE_LIMIT = 100

#: The largest page size a pagination accepts by default.
DEFAULT_MAX_PAGE_LIMIT = 1000

#: The largest page size a pagination may be configured to accept at all - a sanity ceiling
#: against a typo such as ``max_limit=1000000``.
MAX_PAGE_LIMIT_CEILING = 100_000

DEFAULT_LIMIT_PARAMETER = "limit"

DEFAULT_OFFSET_PARAMETER = "offset"

DEFAULT_CURSOR_PARAMETER = "cursor"
