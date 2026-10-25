from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.ddl.conditions.tenant_condition import TenantCondition
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.migrations.autodetection.diffs.declared_schema_object_diff import DeclaredSchemaObjectDiff
from hare.migrations.operations import AddPolicy, AlterPolicy, HareOperation, RemovePolicy, RenamePolicy
from hare.migrations.operations.schema_objects.object_types.policy_object_type import PolicyObjectType
from hare.migrations.operations.schema_objects.object_types.schema_object_type import SchemaObjectType
from hare.models.enums import ModelOption

if TYPE_CHECKING:
    from hare.ddl.security.policy import Policy


class PolicyDiff(DeclaredSchemaObjectDiff):
    """The row level security policy operations of a model - a policy reading a column the migration
    changes is removed before and added after."""

    object_type: ClassVar[type[SchemaObjectType]] = PolicyObjectType
    add_operation_class: ClassVar[type[HareOperation]] = AddPolicy
    alter_operation_class: ClassVar[type[HareOperation] | None] = AlterPolicy
    remove_operation_class: ClassVar[type[HareOperation]] = RemovePolicy
    rename_operation_class: ClassVar[type[HareOperation] | None] = RenamePolicy

    def get_read_columns(self, schema_object: Any) -> tuple[tuple[str, str], ...]:
        """The columns a policy's conditions read - the fields of a ``Q``, the tenant field of
        ``TenantCondition()``; a raw SQL condition is the user's own.

        Args:
            schema_object: The policy.

        Returns:
            ``(table, column)`` of each column, by the names of the state before the migration.
        """
        policy = cast("Policy", schema_object)
        field_names: set[str] = set()
        for condition in (policy.using, policy.with_check):
            if isinstance(condition, TenantCondition):
                tenant_field_name = self.old_state.options.get(ModelOption.TENANT_FIELD)
                if tenant_field_name:
                    field_names.add(tenant_field_name)
            elif condition is not None and not isinstance(condition, RawSQLTerm):
                field_names |= condition.get_referenced_field_names()
        return self.get_field_columns(field_names)
