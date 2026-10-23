from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING

from hare.ddl.enums import GrantTarget
from hare.dialects.postgresql.constants import (
    POSTGRESQL_FUNCTIONS_SQL,
    POSTGRESQL_INSPECTED_FUNCTION_PRIVILEGES_SQL,
    POSTGRESQL_INSPECTED_SEQUENCE_PRIVILEGES_SQL,
    POSTGRESQL_UNIQUE_INDEX_COLUMNS_SQL,
    POSTGRESQL_VIEW_DEFINITIONS_SQL,
)
from hare.dialects.postgresql.introspection.postgresql_inspected_schema_objects import (
    PostgresqlInspectedSchemaObjects,
)
from hare.inspectdb.introspection.enums import SchemaPartType
from hare.inspectdb.introspection.schema_part import SchemaPart

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient


class PostgresqlObjectParts:
    """The views, materialized views and functions of a schema as PostgreSQL prints them - a view's query
    by ``pg_get_viewdef()``, a materialized view's with its unique indexes, a function's every overload
    with its arguments, result, body, language, volatility and security - and the grants on its sequences
    and functions."""

    @classmethod
    async def fetch(
        cls, connection: DatabaseClient, schema: str, object_names: Mapping[SchemaPartType, Collection[str]]
    ) -> list[SchemaPart]:
        """Reads the named objects of a schema.

        Args:
            connection: The connection.
            schema: The schema.
            object_names: The objects' names, by their part type.

        Returns:
            One part per object found.
        """
        parts: list[SchemaPart] = []
        view_names = [
            *object_names.get(SchemaPartType.VIEW, ()),
            *object_names.get(SchemaPartType.MATERIALIZED_VIEW, ()),
        ]
        if view_names:
            for row in await connection.execute_dicts(
                POSTGRESQL_VIEW_DEFINITIONS_SQL, [schema, sorted(set(view_names))]
            ):
                name = row["name"]
                if not row["is_materialized"]:
                    if name in object_names.get(SchemaPartType.VIEW, ()):
                        parts.append(SchemaPart(SchemaPartType.VIEW, "", name, {"query": row["definition"]}))
                    continue
                if name not in object_names.get(SchemaPartType.MATERIALIZED_VIEW, ()):
                    continue
                unique_index_rows = await connection.execute_dicts(POSTGRESQL_UNIQUE_INDEX_COLUMNS_SQL, [schema, name])
                definition = {
                    "query": row["definition"],
                    "unique_indexes": sorted(list(index_row["columns"]) for index_row in unique_index_rows),
                }
                parts.append(SchemaPart(SchemaPartType.MATERIALIZED_VIEW, "", name, definition))
        function_names = sorted(set(object_names.get(SchemaPartType.FUNCTION, ())))
        if function_names:
            overloads_by_name: dict[str, list[dict[str, str | bool]]] = {}
            for row in await connection.execute_dicts(POSTGRESQL_FUNCTIONS_SQL, [schema, function_names]):
                overloads_by_name.setdefault(row["name"], []).append(
                    {
                        "arguments": str(row["full_arguments"]),
                        "returns": str(row["returns"]),
                        "body": str(row["body"]),
                        "language": str(row["language"]),
                        "volatility": str(row["volatility"]),
                        "security_definer": bool(row["security_definer"]),
                    }
                )
            parts.extend(
                SchemaPart(
                    SchemaPartType.FUNCTION,
                    "",
                    name,
                    {"overloads": sorted(overloads, key=lambda overload: str(overload["arguments"]))},
                )
                for name, overloads in overloads_by_name.items()
            )
        sequence_privilege_rows = await connection.execute_dicts(
            POSTGRESQL_INSPECTED_SEQUENCE_PRIVILEGES_SQL, [schema]
        )
        function_privilege_rows = await connection.execute_dicts(
            POSTGRESQL_INSPECTED_FUNCTION_PRIVILEGES_SQL, [schema]
        )
        for grant in (
            *PostgresqlInspectedSchemaObjects.get_object_grants(sequence_privilege_rows, GrantTarget.SEQUENCE),
            *PostgresqlInspectedSchemaObjects.get_object_grants(function_privilege_rows, GrantTarget.FUNCTION),
        ):
            # A grant has no name of its own - it is what it grants.
            definition = SchemaPart.get_definition(grant)
            parts.append(SchemaPart(SchemaPartType.GRANT, "", repr(definition), definition, grant))
        return parts
