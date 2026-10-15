from __future__ import annotations

#: The escape character of hare's LIKE patterns, and what GLOB writes for each LIKE wildcard; a GLOB
#: wildcard inside the matched text is matched by a bracket of its own.
SQLITE_LIKE_ESCAPE_CHARACTER = "\\"
SQLITE_GLOB_WILDCARDS_BY_LIKE_WILDCARD = {"%": "*", "_": "?"}
SQLITE_GLOB_SPECIAL_CHARACTERS = frozenset("*?[")
