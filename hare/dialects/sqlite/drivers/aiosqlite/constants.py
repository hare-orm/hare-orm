from __future__ import annotations

#: The logger aiosqlite writes to.
AIOSQLITE_LIBRARY_LOGGER_NAME = "aiosqlite"

#: The formats of the records aiosqlite writes of each call it runs - the call with its arguments, a
#: statement's values among them.
AIOSQLITE_CALL_LOG_FORMATS = frozenset({"executing %s", "operation %s completed"})

#: aiosqlite's message for any call on a connection that has already been closed.
AIOSQLITE_NO_ACTIVE_CONNECTION_MESSAGE = "no active connection"
