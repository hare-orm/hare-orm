from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from hare.ddl.enums import FunctionVolatility
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.database_function import DatabaseFunction
from hare.dialects.postgresql.constants import POSTGRESQL_DECLARED_FUNCTIONS_SQL, POSTGRESQL_VOLATILITY_CODES
from hare.dialects.postgresql.schema.constants import POSTGRESQL_DEFAULT_FUNCTION_LANGUAGE

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient


class PostgresqlDeclaredFunctions:
    """The functions a model declares as PostgreSQL has them: a function the database lacks is left
    out, one whose body, language, volatility, security or result is another comes with the
    database's."""

    @classmethod
    async def fetch(
        cls,
        connection: DatabaseClient,
        schema: str,
        functions: Sequence[DatabaseFunction],
        get_canonical_type: Callable[[str], str | None],
    ) -> tuple[DatabaseFunction, ...]:
        """Returns the functions of a model as the database has them.

        Args:
            connection: The connection.
            schema: The schema of the model.
            functions: The functions the model declares.
            get_canonical_type: The name PostgreSQL reports a type under - None for one it doesn't know.

        Returns:
            The functions.
        """
        if not functions:
            return ()
        rows = await connection.execute_dicts(
            POSTGRESQL_DECLARED_FUNCTIONS_SQL, [schema, [function.name for function in functions]]
        )
        rows_by_name: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            rows_by_name.setdefault(row["name"], []).append(row)
        observed_functions = []
        for function in functions:
            matching_row = cls.get_matching_row(function, rows_by_name.get(function.name, []))
            if matching_row is not None:
                observed_functions.append(cls.get_observed_function(function, matching_row, get_canonical_type))
        return tuple(observed_functions)

    @staticmethod
    def get_matching_row(function: DatabaseFunction, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        """The function of the database a declared one is - the one of its arguments, or the only one
        of its name.

        Args:
            function: The declared function.
            rows: The database's functions of its name.

        Returns:
            The function's row; None when there is none, or several of other arguments.
        """
        declared_arguments = " ".join(", ".join(function.arguments).lower().split())
        for row in rows:
            if " ".join(str(row["arguments"]).lower().split()) == declared_arguments:
                return row
        return rows[0] if len(rows) == 1 else None

    @staticmethod
    def get_observed_function(
        function: DatabaseFunction, row: dict[str, Any], get_canonical_type: Callable[[str], str | None]
    ) -> DatabaseFunction:
        """A declared function with what the database keeps of it, where it is another.

        Args:
            function: The declared function.
            row: The database's function.
            get_canonical_type: The name PostgreSQL reports a type under.

        Returns:
            The function itself when the database keeps it as declared.
        """
        changes: dict[str, Any] = {}
        if str(row["body"]).strip() != function.body.sql.strip():
            changes["body"] = RawSQLTerm(str(row["body"]))
        if str(row["language"]) != (function.language or POSTGRESQL_DEFAULT_FUNCTION_LANGUAGE).lower():
            changes["language"] = str(row["language"])
        observed_volatility = FunctionVolatility(POSTGRESQL_VOLATILITY_CODES[str(row["volatility"])])
        if observed_volatility != FunctionVolatility(function.volatility):
            changes["volatility"] = observed_volatility
        if bool(row["security_definer"]) != function.security_definer:
            changes["security_definer"] = bool(row["security_definer"])
        declared_result = get_canonical_type(function.returns)
        observed_result = get_canonical_type(str(row["returns"]))
        if declared_result is not None and observed_result is not None and declared_result != observed_result:
            changes["returns"] = str(row["returns"])
        return dataclasses.replace(function, **changes) if changes else function
