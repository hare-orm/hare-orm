from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.ddl.enums import GrantTarget
from hare.dialects.postgresql.introspection.postgresql_object_grants import PostgresqlObjectGrants
from hare.inspectdb.introspection.enums import SchemaPartType
from hare.inspectdb.introspection.schema_part import SchemaPart

if TYPE_CHECKING:
    from hare.inspectdb.introspection.catalog_snapshot import CatalogSnapshot


class PostgresqlObjectParts:
    """The views, materialized views and functions of a schema as PostgreSQL prints them - a view's query
    by ``pg_get_viewdef()``, a materialized view's with its unique indexes, a function's every overload
    with its arguments, result, body, language, volatility and security - and the grants on its sequences
    and functions."""

    @staticmethod
    def get(snapshot: CatalogSnapshot) -> list[SchemaPart]:
        """Build comparison parts from a reusable raw object snapshot, without IO."""
        records = snapshot.copy_records()
        object_names = records["requested"][0]["names"]
        parts: list[SchemaPart] = []
        view_names = [
            *object_names.get(SchemaPartType.VIEW, ()),
            *object_names.get(SchemaPartType.MATERIALIZED_VIEW, ()),
        ]
        if view_names:
            for row in records["views"]:
                name = row["name"]
                if not row["is_materialized"]:
                    if name in object_names.get(SchemaPartType.VIEW, ()):
                        parts.append(SchemaPart(SchemaPartType.VIEW, "", name, {"query": row["definition"]}))
                    continue
                if name not in object_names.get(SchemaPartType.MATERIALIZED_VIEW, ()):
                    continue
                unique_index_rows = row["unique_indexes"]
                definition = {
                    "query": row["definition"],
                    "unique_indexes": sorted(list(index_row["columns"]) for index_row in unique_index_rows),
                }
                parts.append(SchemaPart(SchemaPartType.MATERIALIZED_VIEW, "", name, definition))
        function_names = sorted(set(object_names.get(SchemaPartType.FUNCTION, ())))
        overloads_by_name: dict[str, list[dict[str, Any]]] = {}
        if function_names:
            for row in records["functions"]:
                if row["language"] not in ("sql", "plpgsql"):
                    row["literal_body"] = row.pop("body")
                overloads_by_name.setdefault(row["name"], []).append(
                    {
                        "arguments": str(row["full_arguments"]),
                        "identity_arguments": str(row["arguments"]),
                        **{
                            key: value
                            for key, value in row.items()
                            if key not in ("name", "arguments", "full_arguments")
                        },
                    }
                )
            parts.extend(
                SchemaPart(
                    SchemaPartType.FUNCTION,
                    "",
                    name,
                    {"overloads": sorted(overloads, key=lambda overload: str(overload["arguments"]))},
                    operation_signatures=tuple(sorted(overload["identity_arguments"] for overload in overloads)),
                )
                for name, overloads in overloads_by_name.items()
            )
        sequence_privilege_rows = records["sequence_privileges"]
        function_privilege_rows = records["function_privileges"]
        parts.extend(PostgresqlObjectGrants.get_parts(sequence_privilege_rows, GrantTarget.SEQUENCE))
        parts.extend(
            PostgresqlObjectGrants.get_parts(
                function_privilege_rows,
                GrantTarget.FUNCTION,
                function_signatures={
                    name: tuple(overload["identity_arguments"] for overload in overloads)
                    for name, overloads in overloads_by_name.items()
                },
            )
        )
        return parts
