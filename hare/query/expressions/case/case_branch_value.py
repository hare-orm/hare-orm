from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal

from hare.query.expressions.arithmetic.combined_expression import CombinedExpression
from hare.query.expressions.f import F
from hare.query.expressions.function import Function

#: What `When(then=...)`/`Case(default=...)` take: an expression or a bare Python literal.
CaseBranchValue = (
    str | int | float | bool | Decimal | date | datetime | time | uuid.UUID | None | F | CombinedExpression | Function
)
