from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.fields.field import Field
    from hare.sql.terms.term import Term


@dataclass(frozen=True, slots=True)
class ProjectionColumn:
    key: tuple[Any, ...]
    name: str
    term: Term
    field: Field[Any] | None
