from __future__ import annotations

from hare.ddl.enums import RowLevelSecurity
from hare.ddl.security.policy import Policy
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.exceptions import UnSupportedError
from hare.models import Model


class RowLevelSecurityPolicies(SchemaEditorPart):
    """Row level security of a table and the policies a model declares: switched on and off, created,
    dropped, altered and renamed."""

    __slots__ = ()

    def get_row_level_security_sqls(
        self,
        model: type[Model],
        old_setting: RowLevelSecurity | None,
        new_setting: RowLevelSecurity | None,
    ) -> list[str]:
        """The statements turning a table's row level security from one setting to another.

        Args:
            model: The model.
            old_setting: The setting as it is - None for off.
            new_setting: The setting as it becomes - None for off.

        Raises:
            UnSupportedError: The dialect has no row level security.
        """
        raise UnSupportedError(f"Row level security is not supported on {self.editor.client.dialect}")

    async def alter_row_level_security(
        self,
        model: type[Model],
        old_setting: RowLevelSecurity | None,
        new_setting: RowLevelSecurity | None,
    ) -> None:
        """Turns a table's row level security from one setting to another."""
        await self.editor.run_sqls(self.get_row_level_security_sqls(model, old_setting, new_setting))

    def get_policy_create_sqls(self, model: type[Model], policy: Policy, safe: bool = False) -> list[str]:
        """The statements creating a row level security policy on a model's table.

        Args:
            model: The model declaring it.
            policy: The policy.
            safe: Replace a policy of the same name.

        Raises:
            UnSupportedError: The dialect has no row level security.
        """
        raise self.get_unsupported_error("Row level security policies")

    async def create_policy(self, model: type[Model], policy: Policy) -> None:
        """Creates a row level security policy on a model's table."""
        await self.editor.run_sqls(self.get_policy_create_sqls(model, policy))

    async def drop_policy(self, model: type[Model], policy: Policy) -> None:
        """Drops a row level security policy of a model's table.

        Raises:
            UnSupportedError: The dialect has no row level security.
        """
        raise self.get_unsupported_error("Row level security policies")

    async def alter_policy(self, model: type[Model], old_policy: Policy, new_policy: Policy) -> None:
        """Changes a row level security policy of a model's table in place.

        Raises:
            UnSupportedError: The dialect has no row level security.
        """
        raise self.get_unsupported_error("Row level security policies")

    async def rename_policy(self, model: type[Model], old_policy: Policy, new_policy: Policy) -> None:
        """Renames a row level security policy of a model's table.

        Raises:
            UnSupportedError: The dialect has no row level security.
        """
        raise self.get_unsupported_error("Row level security policies")
