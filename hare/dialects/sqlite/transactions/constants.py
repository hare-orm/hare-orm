from __future__ import annotations

#: Turn SQLite's connection-wide write refusal on for a `Transactions.atomic(read_only=True)` block,
#: and back off before that transaction ends.
SQLITE_ENABLE_QUERY_ONLY_SQL = "PRAGMA query_only = ON"
SQLITE_DISABLE_QUERY_ONLY_SQL = "PRAGMA query_only = OFF"
#: Read and set the connection's busy timeout - how long a statement waits for another
#: connection's lock - for a lock timeout, in whole milliseconds.
SQLITE_BUSY_TIMEOUT_SQL = "PRAGMA busy_timeout"
SQLITE_SET_BUSY_TIMEOUT_SQL = "PRAGMA busy_timeout = {milliseconds}"
