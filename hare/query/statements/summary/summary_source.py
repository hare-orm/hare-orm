from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.query.queryset.query_specification import QuerySpecification
    from hare.query.statements.awaitable_query import AwaitableQuery


@dataclass(frozen=True, slots=True)
class SummarySource:
    """The selected rows of a summary, before constructing its executor.

    The source owns scopes, connection selection and the outer slice. A rows query
    supplies a derived table when needed. Only a QuerySet source may hand its call
    signature to the summary; the description contains no converted row values.
    """

    source: QuerySpecification[Any]
    rows_query: AwaitableQuery[Any] | None = None
    keeps_calls: bool = False
