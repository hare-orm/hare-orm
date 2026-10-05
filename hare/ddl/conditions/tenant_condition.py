from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.classes.class_path import ClassPath
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


@dataclass(frozen=True)
class TenantCondition:
    """The condition of a ``Policy`` keeping a ``Meta.tenant_field`` model's rows to the tenants of
    the transaction - the ``Tenancy.scope()`` active when the transaction began on a connection
    with ``tenant_row_level_security``. A transaction with no scope sees and writes no row."""

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        return ClassPath.get(self.__class__), [], {}

    def get_referenced_field_names(self) -> set[str]:
        """The fields the condition names - none: it reads ``Meta.tenant_field``."""
        return set()

    def get_condition_sql(self, model: type[Model], client: DatabaseClient) -> str:
        """The predicate written into a policy of ``model``'s table.

        Args:
            model: The model the policy belongs to.
            client: The client of the database the DDL runs on.

        Returns:
            The SQL predicate, made by the dialect's schema editor.

        Raises:
            ConfigurationError: The model has no ``Meta.tenant_field``.
            UnSupportedError: The database has no row level security.
        """
        meta = model._meta
        if meta.tenant_field is None:
            raise ConfigurationError(f"{model.__name__}: TenantCondition needs Meta.tenant_field")
        field = meta.fields_map[meta.tenant_field]
        quoted_column = client.dialect.literals.quote_identifier(meta.fields_db_projection[meta.tenant_field])
        return client.dialect.schema_editor_class.tenant_conditions_class.get_tenant_condition_sql(
            quoted_column, field.get_column_type(client.dialect)
        )
