from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


@dataclass(frozen=True, slots=True)
class DeletionScope:
    """An explicit schema visit and the database identity of rows seen during deletion."""

    connection: DatabaseClient
    schema_name: str
    tenant: str
    tables: frozenset[str]

    current: ClassVar[ContextVar[DeletionScope | None]] = ContextVar("hare_deletion_scope", default=None)

    def includes(self, client: DatabaseClient) -> bool:
        """Matches this visit through nested wrappers without matching another database or schema."""
        return (
            self.get_database(client) is self.get_database(self.connection)
            and client.tenant_schema == self.schema_name
        )

    @staticmethod
    def get_database(client: DatabaseClient) -> DatabaseClient:
        """Returns the physical client's identity independently of transaction nesting."""
        return client.get_non_transactional_client() if client.is_transaction_client else client  # type: ignore[attr-defined]

    @staticmethod
    def get_row_identity(
        model: type[Model],
        pk: Any,
        connection: DatabaseClient | None,
    ) -> tuple[DatabaseClient, str | None, type[Model], Any, Any]:
        """Separates equal keys in different clients, tenant schemas and non-unique tenant scopes."""
        from hare.models.deletion.cascade.related_rows import RelatedRows
        from hare.models.tenancy.tenancy import Tenancy
        from hare.query.scopes.tenants.tenant_values import TenantValues

        client = RelatedRows.get_connection_for(model, connection) or model.get_connection(for_write=True)
        root = DeletionScope.get_database(client)
        schema = client.tenant_schema if model._meta.tenant_schema else None
        tenant = (
            TenantValues.get_single_value_or_scope(Tenancy.get_scope(model))
            if model._meta.tenant_field and not client.features.supports_unique_constraints
            else None
        )
        return root, schema, model, pk, tenant
