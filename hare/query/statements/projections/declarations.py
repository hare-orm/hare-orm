from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.fields.field import Field
    from hare.sql.builder.queries.query_builder import QueryBuilder
    from hare.sql.terms.term import Term


type SelectedOutputColumn = tuple[str | None, str | None, str | None, Field[Any] | None]


@dataclass(frozen=True, slots=True)
class ProjectionColumn:
    name: str
    term: Term


@dataclass(frozen=True, slots=True)
class ProjectionAliases:
    query: QueryBuilder
    output_aliases: dict[str, str] | None = None
    source_names: tuple[str | None, ...] = ()
