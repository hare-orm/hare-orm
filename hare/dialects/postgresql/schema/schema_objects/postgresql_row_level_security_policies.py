from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.conditions.constraint_condition import ConstraintCondition
from hare.ddl.enums import PolicyCommand, RowLevelSecurity
from hare.ddl.security.policy import Policy
from hare.dialects.base.schema.schema_objects.row_level_security_policies import RowLevelSecurityPolicies
from hare.dialects.postgresql.schema.constants import (
    POSTGRESQL_POLICY_ALTER_TEMPLATE,
    POSTGRESQL_POLICY_COMMAND_KEYWORDS,
    POSTGRESQL_POLICY_CREATE_TEMPLATE,
    POSTGRESQL_POLICY_DROP_IF_EXISTS_TEMPLATE,
    POSTGRESQL_POLICY_DROP_TEMPLATE,
    POSTGRESQL_POLICY_RENAME_TEMPLATE,
    POSTGRESQL_ROW_LEVEL_SECURITY_TEMPLATE,
)

if TYPE_CHECKING:
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor
    from hare.models.model_binding import ModelBinding


class PostgresqlRowLevelSecurityPolicies(RowLevelSecurityPolicies):
    """RowLevelSecurityPolicies as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    def get_row_level_security_sqls(
        self,
        binding: ModelBinding,
        old_setting: RowLevelSecurity | None,
        new_setting: RowLevelSecurity | None,
    ) -> list[str]:
        table = self.editor.get_model_table_sql(binding)
        actions: list[str] = []
        if new_setting is not None and old_setting is None:
            actions.append("ENABLE")
        if new_setting is None and old_setting is not None:
            actions.append("DISABLE")
        old_forced = old_setting == RowLevelSecurity.FORCED
        new_forced = new_setting == RowLevelSecurity.FORCED
        if new_forced and not old_forced:
            actions.append("FORCE")
        if old_forced and not new_forced:
            actions.append("NO FORCE")
        return [POSTGRESQL_ROW_LEVEL_SECURITY_TEMPLATE.format(table=table, action=action) for action in actions]

    def get_policy_create_sqls(self, binding: ModelBinding, policy: Policy, safe: bool = False) -> list[str]:
        table = self.editor.get_model_table_sql(binding)
        using, with_check = self.get_policy_conditions_sql(binding, policy)
        statements = []
        if safe:
            statements.append(
                POSTGRESQL_POLICY_DROP_IF_EXISTS_TEMPLATE.format(policy=self.editor.quote(policy.name), table=table)
            )
        statements.append(
            POSTGRESQL_POLICY_CREATE_TEMPLATE.format(
                policy=self.editor.quote(policy.name),
                table=table,
                policy_type="PERMISSIVE" if policy.permissive else "RESTRICTIVE",
                command=POSTGRESQL_POLICY_COMMAND_KEYWORDS[PolicyCommand(policy.command)],
                roles=self.editor.get_roles_sql(policy.roles),
                using=using,
                with_check=with_check,
            )
        )
        return statements

    async def drop_policy(self, binding: ModelBinding, policy: Policy) -> None:
        await self.editor.run_sql(
            POSTGRESQL_POLICY_DROP_TEMPLATE.format(
                policy=self.editor.quote(policy.name), table=self.editor.get_model_table_sql(binding)
            )
        )

    async def alter_policy(self, binding: ModelBinding, old_policy: Policy, new_policy: Policy) -> None:
        """``ALTER POLICY`` for new roles or conditions; a new command or type, or a condition taken
        away, drops the policy and creates the new one - ``ALTER POLICY`` changes neither."""
        if (
            old_policy.command != new_policy.command
            or old_policy.permissive != new_policy.permissive
            or (old_policy.using is not None and new_policy.using is None)
            or (old_policy.with_check is not None and new_policy.with_check is None)
        ):
            statements = self.get_policy_create_sqls(binding, new_policy)
            await self.drop_policy(binding, old_policy)
            await self.editor.run_sqls(statements)
            return
        using, with_check = self.get_policy_conditions_sql(binding, new_policy)
        await self.editor.run_sql(
            POSTGRESQL_POLICY_ALTER_TEMPLATE.format(
                policy=self.editor.quote(new_policy.name),
                table=self.editor.get_model_table_sql(binding),
                roles=self.editor.get_roles_sql(new_policy.roles),
                using=using,
                with_check=with_check,
            )
        )

    async def rename_policy(self, binding: ModelBinding, old_policy: Policy, new_policy: Policy) -> None:
        if old_policy.name == new_policy.name:
            return
        await self.editor.run_sql(
            POSTGRESQL_POLICY_RENAME_TEMPLATE.format(
                policy=self.editor.quote(old_policy.name),
                table=self.editor.get_model_table_sql(binding),
                new_name=self.editor.quote(new_policy.name),
            )
        )

    def get_policy_conditions_sql(self, binding: ModelBinding, policy: Policy) -> tuple[str, str]:
        """A policy's ``USING`` and ``WITH CHECK`` clauses - each empty without its condition."""
        using = (
            f" USING ({ConstraintCondition.get_sql(policy.using, binding, self.editor.client)})"
            if policy.using is not None
            else ""
        )
        with_check = (
            f" WITH CHECK ({ConstraintCondition.get_sql(policy.with_check, binding, self.editor.client)})"
            if policy.with_check is not None
            else ""
        )
        return using, with_check
