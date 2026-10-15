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

if TYPE_CHECKING:  # pragma: nocoverage
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
            The scope, and the statement - None under no scope or a scope given model by model,
            which leave the transaction with no tenant.
        """
        scope = RowVisibility.active_tenant.get()
        scope_class = type(scope)
        if scope is None or scope_class is ModelTenants:
            return scope, None
        tenant_texts: list[str] | None
        if scope_class is AllTenants:
            tenant_texts = None
        elif scope_class is TenantValues:
            tenant_texts = [str(value) for value in scope.values]
        else:
            tenant_texts = [str(scope)]
        return scope, client.dialect.transactions.get_tenant_setting_sql(tenant_texts)

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
                f"'{client.connection_alias}' has no tenant_row_level_security"
            )
        if not client.is_transaction_client:
            return QueryError(
                f"{model.__name__} has a TenantCondition policy - its rows are reached inside a transaction "
                "begun in Tenancy.scope(), which sets the transaction's tenants"
            )
        return QueryError(
            f"{model.__name__} has a TenantCondition policy - the transaction began with no tenant scope "
            "(or a scope given model by model) and sees none of its rows; begin it inside Tenancy.scope()"
        )
