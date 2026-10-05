from __future__ import annotations

from collections.abc import (
    Generator,
)
from typing import Any, Literal, Protocol, TypeVar, overload

from hare.query.expressions import (
    Expression,
)
from hare.query.relation_loading.prefetching.prefetch import Prefetch
from hare.query.relation_loading.select import Select
from hare.sql.terms.term import Term

TResult = TypeVar("TResult", covariant=True)


class QuerySetSingle(Protocol[TResult]):
    """A query of one row - awaited, it gives one object, not a list."""

    def __await__(self) -> Generator[Any, None, TResult]: ...

    def prefetch_related(self, *args: str | Prefetch) -> QuerySetSingle[TResult]: ...

    def select_related(self, *args: str | Select) -> QuerySetSingle[TResult]: ...

    def annotate(self, **kwargs: Expression | Term) -> QuerySetSingle[TResult]: ...

    def alias(self, **kwargs: Expression | Term) -> QuerySetSingle[TResult]: ...

    def only(self, *fields_for_select: str) -> QuerySetSingle[TResult]: ...

    def defer(self, *fields: str) -> QuerySetSingle[TResult]: ...

    @overload
    def values_list(
        self,
        *fields_: str,
        flat: Literal[False] = False,
        named: Literal[False] = False,
        **kwargs: Expression | Term,
    ) -> QuerySetSingle[tuple[Any, ...]]: ...

    @overload
    def values_list(
        self, *fields_: str, flat: bool = False, named: bool = False, **kwargs: Expression | Term
    ) -> QuerySetSingle[Any]: ...

    def values(self, *args: str, **kwargs: str | Expression | Term) -> QuerySetSingle[dict[str, Any]]: ...
