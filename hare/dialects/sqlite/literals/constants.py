from __future__ import annotations

import string

#: The case folding SQLite tells names apart by - quoted or not, ASCII letters of either case are one name,
#: other letters aren't folded.
SQLITE_IDENTIFIER_CASE_FOLDING = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
