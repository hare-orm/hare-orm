from __future__ import annotations

from hare.dialects.base.clauses.enums import JoinType

#: The keyword of each join ISO SQL writes, by its shared join declaration.
JOIN_KEYWORDS: dict[str, str] = {JoinType.INNER: "JOIN", JoinType.LEFT_OUTER: "LEFT OUTER JOIN"}
