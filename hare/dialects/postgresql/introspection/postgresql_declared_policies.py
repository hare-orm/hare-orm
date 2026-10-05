from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from hare.ddl.enums import PolicyCommand
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.security.policy import Policy
from hare.dialects.postgresql.constants import POSTGRESQL_DECLARED_POLICIES_SQL, POSTGRESQL_PUBLIC_ROLE

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient


class PostgresqlDeclaredPolicies:
    """The row level security policies a model declares as PostgreSQL has them: a policy the
    database lacks is left out, one of another type, command, roles or condition comes with the
    database's. A condition is compared by its literals - the server keeps it in its own words - and
    one written as a ``Q`` or a tenant condition only by whether it is there."""

    @classmethod
    async def fetch(
        cls,
        connection: DatabaseClient,
        schema: str,
        table: str,
        policies: Sequence[Policy],
        predicate_literals_match: Callable[[str, str], bool],
    ) -> tuple[Policy, ...]:
        """Returns the policies of a model as the database has them.

        Args:
            connection: The connection.
            schema: The schema of the model.
            table: The model's table.
            policies: The policies the model declares.
            predicate_literals_match: Whether a predicate the database reports may be a declared one.

        Returns:
            The policies.
        """
        if not policies:
            return ()
        rows = await connection.execute_dicts(POSTGRESQL_DECLARED_POLICIES_SQL, [schema, table])
        rows_by_name = {row["name"]: row for row in rows}
        return tuple(
            cls.get_observed_policy(policy, rows_by_name[policy.name], predicate_literals_match)
            for policy in policies
            if policy.name in rows_by_name
        )

    @classmethod
    def get_observed_policy(
        cls, policy: Policy, row: dict[str, Any], predicate_literals_match: Callable[[str, str], bool]
    ) -> Policy:
        """A declared policy with what the database keeps of it, where it is another.

        Args:
            policy: The declared policy.
            row: The database's policy.
            predicate_literals_match: Whether a predicate the database reports may be a declared one.

        Returns:
            The policy itself when the database keeps it as declared.
        """
        changes: dict[str, Any] = {}
        permissive = str(row["permissive"]).upper() == "PERMISSIVE"
        if permissive != policy.permissive:
            changes["permissive"] = permissive
        command = PolicyCommand(str(row["cmd"]).lower())
        if command != PolicyCommand(policy.command):
            changes["command"] = command
        observed_roles = {
            POSTGRESQL_PUBLIC_ROLE if role.upper() == POSTGRESQL_PUBLIC_ROLE else role for role in row["roles"]
        }
        declared_roles = {
            POSTGRESQL_PUBLIC_ROLE if role.upper() == POSTGRESQL_PUBLIC_ROLE else role
            for role in policy.roles or (POSTGRESQL_PUBLIC_ROLE,)
        }
        if observed_roles != declared_roles:
            changes["roles"] = () if observed_roles == {POSTGRESQL_PUBLIC_ROLE} else tuple(sorted(observed_roles))
        for name, declared_condition, observed_sql in (
            ("using", policy.using, row["qual"]),
            ("with_check", policy.with_check, row["with_check"]),
        ):
            if not cls.condition_matches(declared_condition, observed_sql, predicate_literals_match):
                changes[name] = RawSQLTerm(observed_sql) if observed_sql is not None else None
        return dataclasses.replace(policy, **changes) if changes else policy

    @staticmethod
    def condition_matches(
        declared_condition: Any, observed_sql: str | None, predicate_literals_match: Callable[[str, str], bool]
    ) -> bool:
        """Whether a policy's condition in the database may be the declared one.

        Args:
            declared_condition: The declared condition, None for none.
            observed_sql: The condition the database keeps, None for none.
            predicate_literals_match: Whether a predicate the database reports may be a declared one.

        Returns:
            True when both are missing, or both are there and the declared one is no raw SQL of other
            literals.
        """
        if declared_condition is None or observed_sql is None:
            return declared_condition is None and observed_sql is None
        if not isinstance(declared_condition, RawSQLTerm):
            return True
        return predicate_literals_match(declared_condition.sql, observed_sql)
