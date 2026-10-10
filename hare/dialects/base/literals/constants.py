from __future__ import annotations

#: A character no SQL statement text can carry: the Postgres protocol ends a string at it, so a
#: caller-supplied name or inline literal holding one is rejected before the statement is sent.
SQL_NULL_BYTE = "\x00"
SQL_NULL_BYTE_MESSAGE = "{text!r}: a name or literal written into the SQL text can't contain a null byte ('\\x00')"
