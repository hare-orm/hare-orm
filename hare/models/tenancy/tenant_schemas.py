"""A schema per tenant: the tables of ``Meta.tenant_schema`` models live in each tenant's own schema,
named by the connection's ``tenant_schema_template``, and a single-tenant ``Tenancy.scope()`` works
through a client whose search path starts with that schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import ConfigurationError, QueryError, UnSupportedError, ValidationError
from hare.models.tenancy.constants import (
    TENANT_SCHEMA_PLACEHOLDER,
    TENANT_SCHEMA_TEMPLATE_TEXT_PATTERN,
    TENANT_SCHEMA_VALUE_PATTERN,
)
from hare.query.scopes.row_visibility import RowVisibility
from hare.query.scopes.tenants.all_tenants import AllTenants
from hare.query.scopes.tenants.model_tenants import ModelTenants
from hare.query.scopes.tenants.tenant_values import TenantValues

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient


class TenantSchemas:
    """Creates, drops and lists the tenants' schemas of a connection with ``tenant_schema_template``,
    and tells which tenant schema the active scope works in."""

    @staticmethod
    def get_checked_template(template: Any) -> str:
        """Checks a connection's ``tenant_schema_template``.

        Args:
            template: The template - lower-case letters, digits and ``_`` around one ``{tenant}``.

        Returns:
            The template.

        Raises:
            ConfigurationError: It isn't such a string.
        """
        if (
            not isinstance(template, str)
            or template.count(TENANT_SCHEMA_PLACEHOLDER) != 1
            or not TENANT_SCHEMA_TEMPLATE_TEXT_PATTERN.fullmatch(template.replace(TENANT_SCHEMA_PLACEHOLDER, ""))
        ):
            raise ConfigurationError(
                f"tenant_schema_template takes lower-case letters, digits and _ around one "
                f"{TENANT_SCHEMA_PLACEHOLDER} (tenant_{TENANT_SCHEMA_PLACEHOLDER}), got {template!r}"
            )
        return template

    @staticmethod
    def get_schema_name(client: DatabaseClient, tenant: Any) -> str:
        """The schema of a tenant.

        Args:
            client: A client of the connection.
            tenant: The tenant value - an int, or a string of lower-case letters, digits and ``_``.

        Returns:
            The schema's name.

        Raises:
            ValidationError: The value can't name a schema, or the name is longer than the database
                allows.
        """
        tenant_text = str(tenant) if type(tenant) is int else tenant
        if not isinstance(tenant_text, str) or not TENANT_SCHEMA_VALUE_PATTERN.fullmatch(tenant_text):
            raise ValidationError(
                f"A tenant schema is named after an int or a string of lower-case letters, digits and _, "
                f"got {tenant!r}"
            )
        schema_name = cast("str", client.tenant_schema_template).replace(TENANT_SCHEMA_PLACEHOLDER, tenant_text)
        max_length = client.features.max_identifier_length
        if max_length is not None and len(schema_name.encode()) > max_length:
            raise ValidationError(
                f"The schema {schema_name!r} of tenant {tenant!r} is longer than the {max_length} bytes "
                "a name may take"
            )
        return schema_name

    @staticmethod
    def get_tenant(client: DatabaseClient, schema_name: str) -> str | None:
        """The tenant a schema is named after.

        Args:
            client: A client of the connection.
            schema_name: The schema's name.

        Returns:
            The tenant value as text; None when the template doesn't name the schema.
        """
        prefix, _, suffix = cast("str", client.tenant_schema_template).partition(TENANT_SCHEMA_PLACEHOLDER)
        if len(schema_name) <= len(prefix) + len(suffix):
            return None
        if not schema_name.startswith(prefix) or not schema_name.endswith(suffix):
            return None
        tenant_text = schema_name[len(prefix) : len(schema_name) - len(suffix)]
        return tenant_text if TENANT_SCHEMA_VALUE_PATTERN.fullmatch(tenant_text) else None

    @classmethod
    def get_active_schema_name(cls, client: DatabaseClient) -> str | None:
        """The tenant schema the active ``Tenancy.scope()`` works in.

        Args:
            client: A client of a connection with ``tenant_schema_template``.

        Returns:
            The schema of the scope's tenant; None with no scope, or a scope of several tenants,
            every tenant, or tenants given model by model.

        Raises:
            ValidationError: The scope's tenant can't name a schema.
        """
        scope = RowVisibility.active_tenant.get()
        if scope is None or type(scope) in {TenantValues, AllTenants, ModelTenants}:
            return None
        return cls.get_schema_name(client, scope)

    @classmethod
    async def create(cls, tenant: Any, using: str | None = None, *, create_tables: bool = True) -> str:
        """Creates a tenant's schema - and in it the tables of the connection's ``Meta.tenant_schema``
        models. A project applying migrations creates the schema without tables and runs ``migrate``,
        which brings every tenant schema up to date.

        Args:
            tenant: The tenant value.
            using: The connection's name, optional with a single connection.
            create_tables: Whether to create the tables of the models.

        Returns:
            The schema's name.

        Raises:
            UnSupportedError: The database has no schemas per tenant.
            ConfigurationError: The connection has no ``tenant_schema_template``.
            QueryError: Called inside a tenant scope or a transaction.
            ValidationError: The tenant value can't name a schema.
        """
        client = cls._get_connection(using)
        schema_name = cls.get_schema_name(client, tenant)
        await client.dialect.schema_editor_class(client).schemas.create_schema(schema_name)
        if create_tables:
            await client.get_schema_client(schema_name).generate_schema(safe=True)
        return schema_name

    @classmethod
    async def drop(cls, tenant: Any, using: str | None = None) -> None:
        """Drops a tenant's schema with everything in it.

        Args:
            tenant: The tenant value.
            using: The connection's name, optional with a single connection.

        Raises:
            UnSupportedError: The database has no schemas per tenant.
            ConfigurationError: The connection has no ``tenant_schema_template``.
            QueryError: Called inside a tenant scope or a transaction.
            ValidationError: The tenant value can't name a schema.
        """
        client = cls._get_connection(using)
        schema_name = cls.get_schema_name(client, tenant)
        tenant_client = client.tenant_clients.pop(schema_name, None)
        if tenant_client is not None:
            await tenant_client.close()
        await client.dialect.schema_editor_class(client).schemas.drop_schema(schema_name)

    @classmethod
    async def get_tenants(cls, using: str | None = None) -> list[str]:
        """The tenants with a schema, by the schema names the template gives.

        Args:
            using: The connection's name, optional with a single connection.

        Returns:
            The tenant values as text, sorted.

        Raises:
            UnSupportedError: The database has no schemas per tenant.
            ConfigurationError: The connection has no ``tenant_schema_template``.
            QueryError: Called inside a tenant scope or a transaction.
        """
        client = cls._get_connection(using)
        return [tenant for _, tenant in await cls.get_schemas(client)]

    @classmethod
    async def get_schemas(cls, client: DatabaseClient) -> list[tuple[str, str]]:
        """The tenant schemas of a connection.

        Args:
            client: The connection's own client.

        Returns:
            ``(schema name, tenant)`` pairs, sorted by the schema name.
        """
        # Local import: the introspector's modules import the models.
        from hare.inspectdb.introspection.database_catalog import DatabaseCatalog

        schemas = []
        for schema_name in await DatabaseCatalog.get_schema_names(client):
            tenant = cls.get_tenant(client, schema_name)
            if tenant is not None:
                schemas.append((schema_name, tenant))
        return schemas

    @staticmethod
    def _get_connection(using: str | None) -> DatabaseClient:
        """The connection's own client, outside any tenant scope and transaction.

        Args:
            using: The connection's name, optional with a single connection.

        Raises:
            UnSupportedError: The database has no schemas per tenant.
            ConfigurationError: The connection has no ``tenant_schema_template``.
            QueryError: Called inside a tenant scope or a transaction.
        """
        # Local import: the transactions import the models.
        from hare.transactions.atomic.atomic import Atomic

        client = Atomic.get_connection(using)
        if not client.features.supports_tenant_schemas:
            raise UnSupportedError(f"The {client.dialect} dialect has no schema per tenant")
        if client.tenant_schema_template is None:
            raise ConfigurationError(f"The connection {client.connection_alias!r} has no tenant_schema_template")
        if client.tenant_schema is not None or client.is_transaction_client:
            raise QueryError(
                "A tenant schema is created, dropped and listed outside a tenant scope and a transaction - "
                f"the connection {client.connection_alias!r} is in one"
            )
        return client
