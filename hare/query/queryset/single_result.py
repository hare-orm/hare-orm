from __future__ import annotations

from collections.abc import (
    Generator,
)
from typing import Any, Literal, Protocol, TypeVar, overload

from hare.query.expressions import (
    Expression,
)
from hare.query.relation_loading.prefetch import Prefetch
from hare.query.relation_loading.select import Select
from hare.sql.terms.base.term import Term

TResult = TypeVar("TResult", covariant=True)


class QuerySetSingle(Protocol[TResult]):
    """
    Awaiting on this will resolve a single instance of the Model object, and not a sequence.
    """

    # pylint: disable=W0104
    def __await__(self) -> Generator[Any, None, TResult]: ...  # pragma: nocoverage

    def prefetch_related(self, *args: str | Prefetch) -> QuerySetSingle[TResult]: ...  # pragma: nocoverage

    def select_related(self, *args: str | Select) -> QuerySetSingle[TResult]: ...  # pragma: nocoverage

    def annotate(self, **kwargs: Expression | Term) -> QuerySetSingle[TResult]: ...  # pragma: nocoverage

    def alias(self, **kwargs: Expression | Term) -> QuerySetSingle[TResult]: ...  # pragma: nocoverage

    def only(self, *fields_for_select: str) -> QuerySetSingle[TResult]: ...  # pragma: nocoverage

    def defer(self, *fields: str) -> QuerySetSingle[TResult]: ...  # pragma: nocoverage

    @overload
    def values_list(
        self,
        *fields_: str,
        flat: Literal[False] = False,
        named: Literal[False] = False,
        **kwargs: Expression | Term,
    ) -> QuerySetSingle[tuple[Any, ...]]: ...  # pragma: nocoverage

    @overload
    def values_list(
        self, *fields_: str, flat: bool = False, named: bool = False, **kwargs: Expression | Term
    ) -> QuerySetSingle[Any]: ...  # pragma: nocoverage

    def values(
        self, *args: str, **kwargs: str | Expression | Term
    ) -> QuerySetSingle[dict[str, Any]]: ...  # pragma: nocoverage
