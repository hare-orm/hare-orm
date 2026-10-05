from __future__ import annotations

from hare.ddl.enums import GrantTarget
from hare.ddl.security.grant import Grant
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.models import Model


class Grants(SchemaEditorPart):
    """Privileges granted on a model's table and schema objects: granted, revoked, and granted again to
    an object made anew."""

    __slots__ = ()

    def get_grant_sqls(self, model: type[Model], grant: Grant) -> list[str]:
        """The statements granting privileges on a model's table or on an object it declares.

        Raises:
            UnSupportedError: The dialect has no privileges to grant.
        """
        raise self.get_unsupported_error("Grants")

    def get_revoke_sqls(self, model: type[Model], grant: Grant) -> list[str]:
        """The statements revoking the privileges of a grant.

        Raises:
            UnSupportedError: The dialect has no privileges to revoke.
        """
        raise self.get_unsupported_error("Grants")

    async def add_grant(self, model: type[Model], grant: Grant) -> None:
        """Grants privileges on a model's table or on an object it declares."""
        await self.editor.run_sqls(self.get_grant_sqls(model, grant))

    async def remove_grant(self, model: type[Model], grant: Grant) -> None:
        """Revokes the privileges of a grant."""
        await self.editor.run_sqls(self.get_revoke_sqls(model, grant))

    async def grant_again(self, model: type[Model], target: GrantTarget, object_name: str) -> None:
        """Grants a model's privileges on an object again - created anew, it has none.

        Args:
            model: The model, rendered with its grants.
            target: The type of the object.
            object_name: Its name.
        """
        for grant in model._meta.grants:
            if grant.on == target and grant.object_name == object_name:
                await self.add_grant(model, grant)
