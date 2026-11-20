from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.classes.class_path import ClassPath
from hare.ddl.conditions.dialect_condition import DialectCondition
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models.model_binding import ModelBinding


@dataclass(frozen=True)
class TenantCondition(DialectCondition):
    """The condition of a ``Policy`` keeping a ``Meta.tenant_field`` model's rows to the tenants of
    the transaction - the ``Tenancy.scope()`` active when the transaction began on a connection
    with ``tenant_row_level_security``, under a scope given model by model the tenants it gives the
    model. A transaction with no scope - or with a scope given model by model not naming the model -
    sees and writes no row."""

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        return ClassPath.get(self.__class__), [], {}

    def get_referenced_field_names(self) -> set[str]:
        """The fields the condition names - none: it reads ``Meta.tenant_field``."""
        return set()

    def with_renamed_field(self, old_name: str, new_name: str) -> TenantCondition:
        """The condition as it is - it names no field."""
        return self

    def get_condition_sql(self, binding: ModelBinding, client: DatabaseClient) -> str:
        """The predicate written into a policy of ``model``'s table.

        Args:
            binding: The model the policy belongs to.
            client: The client of the database the DDL runs on.

        Returns:
            The SQL predicate, made by the dialect's schema editor.

        Raises:
            ConfigurationError: The model has no ``Meta.tenant_field``.
            UnSupportedError: The database has no row level security.
        """
        model = binding._model
        if binding.tenant_field is None:
            raise ConfigurationError(f"{model.__name__}: TenantCondition needs Meta.tenant_field")
        field = binding.fields_map[binding.tenant_field]
        quoted_column = client.dialect.literals.quote_identifier(binding.fields_db_projection[binding.tenant_field])
        return client.dialect.schema_editor_class.tenant_conditions_class.get_tenant_condition_sql(
            quoted_column,
            field.get_column_type(client.dialect),
            client.dialect.literals.get_string_literal_sql(TenantCondition.get_table_key(binding)),
        )

    @staticmethod
    def get_table_key(binding: ModelBinding) -> str:
        """The name a transaction gives a table's tenants under in a scope given model by model - the
        table's, after its ``Meta.schema`` when it has one: a table of one schema is not another's.

        Args:
            binding: The model of the table.

        Returns:
            The name.
        """
        return f"{binding.schema}.{binding.db_table}" if binding.schema else binding.db_table
