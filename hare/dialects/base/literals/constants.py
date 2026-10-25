from __future__ import annotations

#: A character no SQL statement text can carry: a wire protocol ends a string at it, so a
#: caller-supplied name or inline literal holding one is rejected before the statement is sent.
SQL_NULL_BYTE = "\x00"
SQL_NULL_BYTE_MESSAGE = "{text!r}: a name or literal written into the SQL text can't contain a null byte ('\\x00')"
#: The same of a value - left out of the message: it may be a sensitive field's.
SQL_NULL_BYTE_VALUE_MESSAGE = "a text value written into the SQL text can't contain a null byte ('\\x00')"
#: How an int with more digits than Python writes as text (``sys.get_int_max_str_digits()``) is
#: refused - no driver binds it and no literal writes it.
SQL_LONG_INT_MESSAGE = "an int of more digits than Python writes as text (sys.get_int_max_str_digits()) can't be bound"
