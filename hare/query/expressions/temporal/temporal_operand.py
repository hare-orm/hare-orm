from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hare.fields.field import Field
from hare.query.expressions.enums import TemporalType
from hare.sql.terms.term import Term


@dataclass(frozen=True)
class TemporalOperand:
    """One side of an arithmetic expression, as seen by the temporal rules.

    Attributes:
        term: The operand's resolved SQL term.
        type: The type of date/time value it holds, or None when it holds none.
        field: The field its value is decoded through, when it has one.
        is_literal: Whether it is a bare Python literal (a ``Value``).
    """

    term: Term
    type: TemporalType | None
    field: Field[Any] | None
    is_literal: bool
