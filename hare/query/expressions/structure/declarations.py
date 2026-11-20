from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hare.query.expressions.structure.enums import ArgumentRole


@dataclass(frozen=True, slots=True)
class ExpressionChild:
    argument: int | str
    value: Any
    role: ArgumentRole = ArgumentRole.VALUE


@dataclass(frozen=True, slots=True)
class FieldReference:
    path: str
    outer_scopes: int = 0
    lookup: bool = False
