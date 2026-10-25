from __future__ import annotations

#: The keyword of each join ISO SQL writes, by the ``JoinType`` value - the SQL builder's enum imports the
#: dialects.
JOIN_KEYWORDS: dict[str, str] = {"inner": "JOIN", "left_outer": "LEFT OUTER JOIN"}
