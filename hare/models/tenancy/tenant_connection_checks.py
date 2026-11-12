from __future__ import annotations

from typing import TYPE_CHECKING

from hare.exceptions import ConfigurationError, HareError, QueryError
from hare.models.tenancy.tenant_row_level_security import TenantRowLevelSecurity

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models.meta_info import MetaInfo


class TenantConnectionChecks:
    """Checks that a model can use a connection under the active tenant schema and RLS scope."""

    @staticmethod
    def check_tenant_client(meta: MetaInfo, client: DatabaseClient) -> None:
        """Checks a query of the model may run on a client - a ``Meta.tenant_schema`` model's on a
        tenant schema's client, a model with a ``TenantCondition`` policy's in a transaction that set
        its tenants. Called only when ``checks_tenant_client`` is set.

        Args:
            client: The client the query would run on.

        Raises:
            ConfigurationError: The connection has no ``tenant_schema_template`` /
                ``tenant_row_level_security``.
            QueryError: No single tenant is active, or the query runs outside a transaction that set
                its tenants.
        """
        if meta.tenant_schema and client.tenant_schema is None:
            raise meta.get_missing_tenant_schema_error(client)
        if meta.tenant_row_level_security and not getattr(client, "sets_tenants", False):
            raise TenantRowLevelSecurity.get_missing_tenants_error(meta._model, client)

    @staticmethod
    def get_missing_tenant_schema_error(meta: MetaInfo, client: DatabaseClient) -> HareError:
        """The error of reaching a ``Meta.tenant_schema`` model's table through a client of no tenant.

        Args:
            client: The client the model's connection gave.

        Returns:
            ConfigurationError when the connection has no schema per tenant, else QueryError - no
            single tenant is active.
        """
        if client.tenant_schema_template is None:
            return ConfigurationError(
                f"{meta._model.__name__} has Meta.tenant_schema, but its connection "
                f"{meta.default_connection!r} has no tenant_schema_template"
            )
        return QueryError(
            f"{meta._model.__name__} has Meta.tenant_schema - its table is reached inside "
            "Tenancy.scope(tenant) of a single tenant"
        )
