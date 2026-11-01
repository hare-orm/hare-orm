"""Tenancy enforced by the database: a ``Meta.tenant_field`` model's ``Policy`` with ``TenantCondition``
lets a transaction see and write only the rows of the tenants active when it began - on a connection
with ``tenant_row_level_security`` every transaction sets them right after ``BEGIN``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.exceptions import ConfigurationError, HareError, QueryError
from hare.query.scopes.row_visibility import RowVisibility
from hare.query.scopes.tenants.all_tenants import AllTenants
from hare.query.scopes.tenants.model_tenants import ModelTenants
from hare.query.scopes.tenants.tenant_values import TenantValues

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


class TenantRowLevelSecurity:
    """Sets a transaction's tenants for the ``TenantCondition`` policies and checks a query of such a
    model runs in a transaction that set them."""

    @staticmethod
    def get_transaction_setting(client: DatabaseClient) -> tuple[Any, str | None]:
        """The tenant scope a transaction begins in and the statement setting its tenants.

        Args:
            client: The transaction's client.

        Returns:
            The scope, and the statement - None under no scope, which leaves the transaction with no
            tenant. A scope given model by model sets the tenants of each table of the connection's
            models with a ``TenantCondition`` policy it names.
        """
        scope = RowVisibility.active_tenant.get()
        if scope is None:
            return scope, None
        if type(scope) is ModelTenants:
            table_tenants = scope.get_policy_tenants(client.connection_alias)
            return scope, client.dialect.transactions.get_table_tenants_setting_sql(
                {
                    table_key: TenantRowLevelSecurity.get_tenant_texts(table_scope)
                    for table_key, table_scope in table_tenants.items()
                }
            )
        return scope, client.dialect.transactions.get_tenant_setting_sql(
            TenantRowLevelSecurity.get_tenant_texts(scope)
        )

    @staticmethod
    def get_tenant_texts(scope: Any) -> list[str] | None:
        """The tenant values of a scope as text.

        Args:
            scope: A tenant value, a ``TenantValues`` or ``Tenancy.ALL``.

        Returns:
            The texts; None for every tenant.
        """
        scope_class = type(scope)
        if scope_class is AllTenants:
            return None
        if scope_class is TenantValues:
            return [str(value) for value in scope.values]
        return [str(scope)]

    @staticmethod
    def get_missing_tenants_error(model: type[Model], client: DatabaseClient) -> HareError:
        """The error of a query of a model with a ``TenantCondition`` policy through a client that set
        no tenants.

        Args:
            model: The model.
            client: The client the query would run on.

        Returns:
            ConfigurationError when the connection has no ``tenant_row_level_security``, else
            QueryError - the query runs outside a transaction, or in one begun under no scope or a
            scope given model by model.
        """
        if not client.tenant_row_level_security:
            return ConfigurationError(
                f"{model.__name__} has a TenantCondition policy, but its connection "
                f"{client.connection_alias!r} has no tenant_row_level_security"
            )
        if not client.is_transaction_client:
            return QueryError(
                f"{model.__name__} has a TenantCondition policy - its rows are reached inside a transaction "
                "begun in Tenancy.scope(), which sets the transaction's tenants"
            )
        return QueryError(
            f"{model.__name__} has a TenantCondition policy - the transaction began with no tenant scope "
            "and sees none of its rows; begin it inside Tenancy.scope()"
        )
