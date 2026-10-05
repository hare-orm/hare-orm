from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Any, ClassVar

from hare.core.caching.cache import Cache
from hare.query.constants import AMBIENT_TENANT_NOT_OVERRIDDEN
from hare.query.scopes.constants import RESOLVED_DEFAULT_ATTRIBUTE


@dataclass(frozen=True, slots=True)
class RowVisibility:
    """Which rows the default scopes of the models a query reads let it see. A queryset holds one;
    every JOIN the query builds is scoped by the same one.

    Args:
        all_tenants: Rows of every tenant.
        include_deleted: Soft-deleted rows too.
        only_deleted: Only the soft-deleted rows of the queried model; related models show their
            deleted rows too.
        tenant: The tenant scope the rows are limited by instead of the one active when the query is
            built - ``AMBIENT_TENANT_NOT_OVERRIDDEN`` for the active one. A query being built holds
            the scope it resolved.
        tenant_is_pinned: ``tenant`` stays as it is when the query is built again.
    """

    all_tenants: bool = False
    include_deleted: bool = False
    only_deleted: bool = False
    tenant: Any = AMBIENT_TENANT_NOT_OVERRIDDEN
    tenant_is_pinned: bool = False

    #: The tenant scope active for the current task, None when none is.
    active_tenant: ClassVar[contextvars.ContextVar[Any | None]] = contextvars.ContextVar(
        "hare_current_tenant", default=None
    )
    #: Every default scope applies, for the active tenant.
    DEFAULT: ClassVar[RowVisibility]
    #: ``DEFAULT`` resolved with no tenant active.
    DEFAULT_WITHOUT_TENANT: ClassVar[RowVisibility]
    #: ``DEFAULT`` resolved for the last tenant it was resolved for - ``(tenant, visibility)``, kept on
    #: the class (``RESOLVED_DEFAULT_ATTRIBUTE``): every query of a request resolves it for one tenant.
    resolved_defaults: ClassVar[Cache[tuple[Any, RowVisibility]]] = Cache(
        holds_sql=False, keyed_by_model=False, owner_attribute=RESOLVED_DEFAULT_ATTRIBUTE
    )

    def get_for_active_tenant(self) -> RowVisibility:
        """This visibility with the tenant it scopes to resolved - the active one, unless a
        tenant is pinned.

        Returns:
            The visibility.
        """
        if self.tenant_is_pinned:
            return self
        tenant = self.active_tenant.get()
        if tenant is self.tenant:
            return self
        if self is RowVisibility.DEFAULT:
            if tenant is None:
                return RowVisibility.DEFAULT_WITHOUT_TENANT
            resolved_default: tuple[Any, RowVisibility] | None = getattr(RowVisibility, RESOLVED_DEFAULT_ATTRIBUTE)
            if resolved_default is not None and resolved_default[0] is tenant:
                return resolved_default[1]
            visibility = RowVisibility(tenant=tenant)
            RowVisibility.resolved_defaults.set_owner_value(RowVisibility, (tenant, visibility))
            return visibility
        return RowVisibility(self.all_tenants, self.include_deleted, self.only_deleted, tenant)

    def get_for_related_query(self) -> RowVisibility:
        """The visibility of a query of another model made for a query with this one: the same
        escape hatches, pinned to the tenant resolved here; ``only_deleted`` is the queried
        model's alone.

        Returns:
            The visibility.
        """
        tenant_is_pinned = self.tenant is not AMBIENT_TENANT_NOT_OVERRIDDEN
        if not self.only_deleted and self.tenant_is_pinned == tenant_is_pinned:
            return self
        return RowVisibility(self.all_tenants, self.include_deleted, False, self.tenant, tenant_is_pinned)


RowVisibility.DEFAULT = RowVisibility()

RowVisibility.DEFAULT_WITHOUT_TENANT = RowVisibility(tenant=None)

setattr(RowVisibility, RESOLVED_DEFAULT_ATTRIBUTE, None)
