from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.sql.builder.returned_value import ReturnedValue
    from hare.sql.builder.tables.table import Table
    from hare.sql.terms.term import Term


@dataclass(frozen=True, slots=True)
class ValuesColumn:
    """Zero-based position in one bound input row, independent of SQL aliases."""

    position: int


@dataclass(frozen=True, slots=True)
class UpdateFromValues:
    """Structural assignments and matching rules for updating from bound input rows.

    Column names are unquoted identifiers. Values reference input-row positions or
    expression trees; renderers choose their own row source and column qualification.
    A condition stays a tree, including opaque raw SQL, until the dialect renders it.
    """

    table: Table
    assignments: tuple[tuple[str, ValuesColumn | Term], ...]
    matches: tuple[tuple[str, ValuesColumn], ...]
    columns: tuple[str, ...]
    alias: str
    condition: Term | None
    returned: tuple[ReturnedValue, ...]
