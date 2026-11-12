from __future__ import annotations

from contextvars import Context, ContextVar, copy_context
from functools import cached_property
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.caching.cache import Cache
from hare.query.expressions import Q

if TYPE_CHECKING:
    from hare.models import Model
    from hare.query.scopes.row_scope_provider import RowScopeProvider
    from hare.query.scopes.row_visibility import RowVisibility


class RowScopeBinding:
    """Binds provider predicates for one execution, including its related queries.

    The holder belongs to an execution, never to a model or a shared queryset. Conditions
    and their context references disappear with it. Independent executions read their
    providers again, even when the SQL plan can be reused.
    """

    current: ClassVar[ContextVar[RowScopeBinding | None]] = ContextVar("hare_row_scope_binding", default=None)
    conditions: ClassVar[Cache[tuple[RowScopeProvider, RowVisibility, Q | None]]] = Cache(holds_sql=False)

    @cached_property
    def cache_buckets(self) -> dict[int, Any]:
        """Allocate storage only when a provider participates in this execution."""
        return {}

    @cached_property
    def context(self) -> Context:
        """Capture the provider context on its first use and reuse it for nested queries."""
        return copy_context()

    @classmethod
    def get_condition(
        cls, provider: RowScopeProvider | None, model: type[Model], visibility: RowVisibility
    ) -> Q | None:
        """Gets a prepared predicate, wrapping expression predicates as ordinary Q conditions."""
        if provider is None:
            return None
        binding = cls.current.get()
        key = (
            model,
            id(provider),
            visibility.all_tenants,
            visibility.include_deleted,
            visibility.only_deleted,
            id(visibility.tenant),
        )
        bucket = cls.conditions.get_bucket(binding) if binding is not None else None
        prepared = None if bucket is None else bucket.get(key)
        if prepared is not None:
            return prepared[2]
        result = (
            provider.get_condition(model, visibility)
            if binding is None
            else binding.context.run(provider.get_condition, model, visibility)
        )
        condition = result if result is None or isinstance(result, Q) else Q.from_expression(result)
        if bucket is not None:
            bucket[key] = (provider, visibility, condition)
        return condition
