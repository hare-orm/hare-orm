from __future__ import annotations

from typing import Any

from hare.ddl.enums import FunctionVolatility, PolicyCommand, Privilege
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.database_function import DatabaseFunction
from hare.ddl.schema_objects.database_sequence import DatabaseSequence
from hare.ddl.security.grant import Grant
from hare.ddl.security.policy import Policy
from hare.dialects.postgresql.constants import POSTGRESQL_PUBLIC_ROLE, POSTGRESQL_VOLATILITY_CODES
from hare.fields.data.constants import INT64_MAX, INT64_MIN
from hare.inspectdb.introspection.catalog_sql import CatalogSql


class PostgresqlInspectedSchemaObjects:
    """The schema objects inspectdb reads of a PostgreSQL schema, as the declarations a model's ``Meta``
    holds: the policies, grants and sequences of a table, and the functions and sequences of no table."""

    @staticmethod
    def get_policies(rows: list[dict[str, Any]]) -> list[Policy]:
        """The policies of a table.

        Args:
            rows: Its POSTGRESQL_INSPECTED_POLICIES_SQL rows.

        Returns:
            The policies, conditions as the server keeps them.
        """
        policies = []
        for row in rows:
            roles = tuple(row["roles"])
            policies.append(
                Policy(
                    name=row["name"],
                    command=PolicyCommand(str(row["cmd"]).lower()),
                    roles=() if {role.upper() for role in roles} == {POSTGRESQL_PUBLIC_ROLE} else roles,
                    using=None if row["qual"] is None else RawSQLTerm(row["qual"]),
                    with_check=None if row["with_check"] is None else RawSQLTerm(row["with_check"]),
                    permissive=str(row["permissive"]).upper() == "PERMISSIVE",
                )
            )
        return policies

    @staticmethod
    def get_grants(
        table_rows: list[dict[str, Any]], column_rows: list[dict[str, Any]]
    ) -> tuple[list[Grant], list[str]]:
        """The grants on a table and its columns - one per role and grant option, a column grant per
        set of columns given the same privileges.

        Args:
            table_rows: Its POSTGRESQL_INSPECTED_TABLE_PRIVILEGES_SQL rows.
            column_rows: Its POSTGRESQL_INSPECTED_COLUMN_PRIVILEGES_SQL rows.

        Returns:
            The grants - the columns named by their column names - and a note per privilege no
            ``Privilege`` names.
        """
        known_privileges = {privilege.value for privilege in Privilege}
        notes: list[str] = []
        table_privileges: dict[tuple[str, bool], list[Privilege]] = {}
        for row in table_rows:
            privilege_name = str(row["privilege_type"]).lower()
            if privilege_name not in known_privileges:
                notes.append(f"privilege {row['privilege_type']} of role {row['role']!r} has no Privilege")
                continue
            table_privileges.setdefault((row["role"], bool(row["is_grantable"])), []).append(Privilege(privilege_name))
        column_privileges: dict[tuple[str, bool], dict[str, list[Privilege]]] = {}
        for row in column_rows:
            privilege_name = str(row["privilege_type"]).lower()
            if privilege_name not in known_privileges:
                notes.append(
                    f"privilege {row['privilege_type']} on column {row['column_name']!r} of role {row['role']!r} "
                    "has no Privilege"
                )
                continue
            privileges_by_column = column_privileges.setdefault((row["role"], bool(row["is_grantable"])), {})
            privileges_by_column.setdefault(row["column_name"], []).append(Privilege(privilege_name))
        grants = [
            Grant(privileges=tuple(privileges), roles=(role,), with_grant_option=grantable)
            for (role, grantable), privileges in table_privileges.items()
        ]
        for (role, grantable), privileges_by_column in column_privileges.items():
            columns_by_privileges: dict[tuple[Privilege, ...], list[str]] = {}
            for column, privileges in privileges_by_column.items():
                columns_by_privileges.setdefault(tuple(privileges), []).append(column)
            grants.extend(
                Grant(privileges=privileges, roles=(role,), columns=tuple(columns), with_grant_option=grantable)
                for privileges, columns in columns_by_privileges.items()
            )
        return grants, notes

    @staticmethod
    def get_sequence(row: dict[str, Any], owned_by: str | None) -> DatabaseSequence:
        """A sequence - each setting PostgreSQL's default for it left out.

        Args:
            row: Its row - start, increment, bounds, cycle and cache.
            owned_by: The field owning it, None for none.

        Returns:
            The sequence.
        """
        increment = int(row["increment_by"])
        ascending = increment > 0
        default_minimum = 1 if ascending else INT64_MIN
        default_maximum = INT64_MAX if ascending else -1
        minimum = int(row["min_value"])
        maximum = int(row["max_value"])
        start = int(row["start_value"])
        return DatabaseSequence(
            name=row["name"],
            start=None if start == (minimum if ascending else maximum) else start,
            increment=increment,
            minimum=None if minimum == default_minimum else minimum,
            maximum=None if maximum == default_maximum else maximum,
            cycle=bool(row["cycle"]),
            cache=int(row["cache_size"]),
            owned_by=owned_by,
        )

    @staticmethod
    def get_function(row: dict[str, Any]) -> DatabaseFunction:
        """A function.

        Args:
            row: Its POSTGRESQL_INSPECTED_FUNCTIONS_SQL row.

        Returns:
            The function.
        """
        return DatabaseFunction(
            name=row["name"],
            returns=str(row["returns"]),
            body=RawSQLTerm(str(row["body"]).strip()),
            arguments=PostgresqlInspectedSchemaObjects.split_arguments(str(row["arguments"])),
            language=str(row["language"]),
            volatility=FunctionVolatility(POSTGRESQL_VOLATILITY_CODES[str(row["volatility"])]),
            security_definer=bool(row["security_definer"]),
        )

    @staticmethod
    def split_arguments(text: str) -> tuple[str, ...]:
        """Split a function's arguments, preserving commas in parentheses, quotes and comments.

        Args:
            text: The declarations and defaults returned by ``pg_get_function_arguments()``.

        Returns:
            The arguments.
        """
        return tuple(argument.strip() for argument in CatalogSql.split_terms(text) if argument.strip())
