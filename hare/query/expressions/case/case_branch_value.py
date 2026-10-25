from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal

from hare.query.expressions.expression import Expression

#: What `When(then=...)`/`Case(default=...)` take: an expression of any sort - a field, a value, a
#: function, a Case, a subquery - or a bare Python literal.
CaseBranchValue = str | int | float | bool | Decimal | date | datetime | time | uuid.UUID | None | Expression
