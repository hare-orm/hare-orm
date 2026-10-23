from __future__ import annotations

#: SQLite's primary result code SQLITE_BUSY - another connection holds a lock the statement needs.
SQLITE_BUSY_RESULT_CODE = 5

#: The bits of an extended SQLite result code that hold its primary result code.
SQLITE_PRIMARY_RESULT_CODE_MASK = 0xFF
