from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from hare.ddl.constants import COLUMN_PRIVILEGES, GRANT_TARGET_PRIVILEGES
from hare.ddl.enums import GrantTarget, Privilege
from hare.ddl.security.grant import Grant
from hare.dialects.postgresql.constants import (
    POSTGRESQL_COLUMN_PRIVILEGES_SQL,
    POSTGRESQL_FUNCTION_PRIVILEGES_SQL,
    POSTGRESQL_PUBLIC_ROLE,
    POSTGRESQL_RELATION_PRIVILEGES_SQL,
)
from hare.dialects.postgresql.schema.constants import POSTGRESQL_ROLE_KEYWORDS

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient


class PostgresqlDeclaredGrants:
    """The grants a model declares as PostgreSQL has them: a grant some of whose privileges a role
    lacks - or holds without the grant option it declares - is left out. A privilege the database
    gives beyond the declared ones isn't compared; a role named by a keyword other than ``PUBLIC``
    (``CURRENT_USER``) has no name to compare."""

    @classmethod
    async def fetch(
        cls,
        connection: DatabaseClient,
        schema: str,
        table: str,
        grants: Sequence[Grant],
        field_to_column_name: Mapping[str, str],
    ) -> tuple[Grant, ...]:
        """Returns the grants of a model the database holds.

        Args:
            connection: The connection.
            schema: The schema of the model.
            table: The model's table.
            grants: The grants the model declares.
            field_to_column_name: Field name -> its column.

        Returns:
            The grants.
        """
        if not grants:
            return ()
        relation_names = [table] + [
            grant.object_name for grant in grants if grant.on != GrantTarget.FUNCTION and grant.object_name is not None
        ]
        function_names = [
            grant.object_name for grant in grants if grant.on == GrantTarget.FUNCTION and grant.object_name is not None
        ]
        held_privileges: dict[tuple[str, str, str], bool] = {}
        rows = await connection.execute_dicts(POSTGRESQL_RELATION_PRIVILEGES_SQL, [schema, relation_names])
        if function_names:
            rows.extend(await connection.execute_dicts(POSTGRESQL_FUNCTION_PRIVILEGES_SQL, [schema, function_names]))
        held_column_privileges: dict[tuple[str, str, str], bool] = {}
        if any(grant.columns for grant in grants):
            for row in await connection.execute_dicts(POSTGRESQL_COLUMN_PRIVILEGES_SQL, [schema, table]):
                cls.add_held_privilege(held_column_privileges, row)
        for row in rows:
            cls.add_held_privilege(held_privileges, row)
        return tuple(
            grant
            for grant in grants
            if cls.holds_grant(grant, table, held_privileges, held_column_privileges, field_to_column_name)
        )

    @staticmethod
    def add_held_privilege(held_privileges: dict[tuple[str, str, str], bool], row: dict[str, object]) -> None:
        """Records a privilege a role holds.

        Args:
            held_privileges: (object or column name, role, privilege) -> whether the role may grant
                it on; added to.
            row: The object, role, privilege and grant option.
        """
        key = (str(row["object_name"]), str(row["role"]), str(row["privilege_type"]).lower())
        held_privileges[key] = held_privileges.get(key, False) or bool(row["is_grantable"])

    @staticmethod
    def holds_grant(
        grant: Grant,
        table: str,
        held_privileges: dict[tuple[str, str, str], bool],
        held_column_privileges: dict[tuple[str, str, str], bool],
        field_to_column_name: Mapping[str, str],
    ) -> bool:
        """Whether every role of a grant holds every privilege of it, with the grant option it
        declares.

        Args:
            grant: The declared grant.
            table: The model's table.
            held_privileges: The privileges held on the model's objects.
            held_column_privileges: The privileges held on the table's columns.
            field_to_column_name: Field name -> its column.

        Returns:
            Whether the database holds the grant.
        """
        target = GrantTarget(grant.on)
        if grant.columns:
            privilege_names = {Privilege(privilege).value for privilege in grant.privileges}
            if Privilege.ALL.value in privilege_names:
                privilege_names = {privilege.value for privilege in COLUMN_PRIVILEGES}
            object_names = [field_to_column_name.get(field_name, field_name) for field_name in grant.columns]
            privileges = held_column_privileges
        else:
            privilege_names = {Privilege(privilege).value for privilege in grant.privileges}
            if Privilege.ALL.value in privilege_names:
                privilege_names = {
                    privilege.value for privilege in GRANT_TARGET_PRIVILEGES[target] if privilege != Privilege.ALL
                }
            object_names = [table if target == GrantTarget.TABLE else str(grant.object_name)]
            privileges = held_privileges
        for role in grant.roles:
            role_name = POSTGRESQL_PUBLIC_ROLE if role.upper() == POSTGRESQL_PUBLIC_ROLE else role
            if role_name != POSTGRESQL_PUBLIC_ROLE and role.upper() in POSTGRESQL_ROLE_KEYWORDS:
                continue
            for object_name in object_names:
                for privilege_name in privilege_names:
                    grantable = privileges.get((object_name, role_name, privilege_name))
                    if grantable is None or (grant.with_grant_option and not grantable):
                        return False
        return True
