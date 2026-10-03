from __future__ import annotations

from typing import Any, cast

from hare.exceptions import QueryError
from hare.query.constants import AMBIENT_TENANT_NOT_OVERRIDDEN
from hare.query.scopes.all_tenants import AllTenants
from hare.query.scopes.model_tenants import ModelTenants
from hare.query.scopes.row_scope import RowScope
from hare.query.scopes.row_visibility import RowVisibility
from hare.query.scopes.tenant_values import TenantValues


class TenantScope(RowScope):
    """``Meta.tenant_field``: only the rows of the tenants the active scope gives the model are
    seen; ``.all_tenants()`` shows every tenant's."""

    __slots__ = ()

    def get_filter(self, visibility: RowVisibility) -> tuple[str, Any] | None:
        """
        Raises:
            QueryError: If the model has no tenant scope.
        """
        if visibility.all_tenants:
            return None
        model = self.model
        tenant = visibility.tenant
        if tenant is AMBIENT_TENANT_NOT_OVERRIDDEN:
            tenant = RowVisibility.active_tenant.get()
        tenant_class = type(tenant)
        names_models = tenant_class is ModelTenants
        if names_models:
            tenant = tenant.get_for_model(model)
            tenant_class = type(tenant)
        if tenant is None:
            reason = (
                f"the active Tenancy.scope(...) doesn't name {model.__name__}"
                if names_models
                else "no tenant is active"
            )
            raise QueryError(
                f"{model.__name__} has Meta.tenant_field '{model._meta.tenant_field}' set but "
                f"{reason} - wrap this call in Tenancy.scope(...), or use "
                f"{model.__name__}.objects.all_tenants() (.all_tenants() on the queried model for a query "
                f"reaching {model.__name__} through a relation, clear(all_tenants=True) on a "
                "many-to-many relation) to intentionally query across every tenant"
            )
        if tenant_class is TenantValues or tenant_class is AllTenants:
            return self.get_scope_filter(cast("str", model._meta.tenant_field), tenant)
        return cast("str", model._meta.tenant_field), tenant

    @staticmethod
    def get_scope_filter(tenant_field: str, scope: Any) -> tuple[str, Any] | None:
        """The filter one model's tenant scope puts on its rows.

        Args:
            tenant_field: The model's ``Meta.tenant_field``.
            scope: The model's scope - a tenant value, a ``TenantValues`` or ``Tenancy.ALL``.

        Returns:
            ``(field, value)`` for one value, ``(field__in, values)`` for several, None for
            ``Tenancy.ALL``.
        """
        scope_class = type(scope)
        if scope_class is TenantValues:
            return f"{tenant_field}__in", list(scope.values)
        if scope_class is AllTenants:
            return None
        return tenant_field, scope
