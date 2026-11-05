from __future__ import annotations

#: The page size a pagination returns when the request names none.
DEFAULT_PAGE_LIMIT = 100

#: The largest page size a pagination accepts by default.
DEFAULT_MAX_PAGE_LIMIT = 1000

DEFAULT_LIMIT_PARAMETER = "limit"

DEFAULT_OFFSET_PARAMETER = "offset"

DEFAULT_CURSOR_PARAMETER = "cursor"

#: Built-in cursor types whose adapters hold no application classes and can be shared safely.
CURSOR_SCALAR_TYPES = (int, str, float, bool, bytes)
