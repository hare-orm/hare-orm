from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.ddl.enums import GrantTarget, SchemaPartType
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.materialized_view import MaterializedView
from hare.ddl.schema_objects.view import View
from hare.dialects.postgresql.introspection.postgresql_inspected_schema_objects import PostgresqlInspectedSchemaObjects
from hare.dialects.postgresql.introspection.postgresql_object_grants import PostgresqlObjectGrants
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
        view_names = object_names.get(SchemaPartType.VIEW, ())
        materialized_view_names = object_names.get(SchemaPartType.MATERIALIZED_VIEW, ())
        if view_names or materialized_view_names:
            for row in records["views"]:
                name = row["name"]
                if not row["is_materialized"]:
                    if name in view_names:
                        query = row["definition"]
                        parts.append(
                            SchemaPart(SchemaPartType.VIEW, "", name, {"query": query}, View(name, RawSQLTerm(query)))
                        )
                    continue
                if name not in materialized_view_names:
                    continue
                unique_index_rows = row["unique_indexes"]
                definition = {
                    "query": row["definition"],
                    "unique_indexes": sorted(list(index_row["columns"]) for index_row in unique_index_rows),
                }
                view = (
                    MaterializedView(
                        name,
                        RawSQLTerm(row["definition"]),
                        unique_columns=tuple(definition["unique_indexes"][0]) if unique_index_rows else (),
                    )
                    if len(unique_index_rows) <= 1
                    else None
                )
                parts.append(SchemaPart(SchemaPartType.MATERIALIZED_VIEW, "", name, definition, view))
        overloads_by_name: dict[str, list[dict[str, Any]]] = {}
        function_signatures: dict[str, tuple[str, ...]] = {}
        functions = {}
        if object_names.get(SchemaPartType.FUNCTION):
            for row in records["functions"]:
                if row["language"] in ("sql", "plpgsql"):
                    if row["body"].strip() and row["returns"]:
                        functions[row["name"]] = PostgresqlInspectedSchemaObjects.get_function(
                            {**row, "arguments": row["full_arguments"]}
                        )
                else:
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
            for name, overloads in overloads_by_name.items():
                signatures = tuple(sorted(overload["identity_arguments"] for overload in overloads))
                function_signatures[name] = signatures
                parts.append(
                    SchemaPart(
                        SchemaPartType.FUNCTION,
                        "",
                        name,
                        {"overloads": sorted(overloads, key=lambda overload: str(overload["arguments"]))},
                        functions.get(name) if len(overloads) == 1 else None,
                        operation_signatures=signatures,
                    )
                )
        sequence_privilege_rows = records["sequence_privileges"]
        function_privilege_rows = records["function_privileges"]
        parts.extend(
            SchemaPart(
                SchemaPartType.SEQUENCE,
                "",
                row["name"],
                row,
                PostgresqlInspectedSchemaObjects.get_sequence(row),
            )
            for row in records["sequences"]
        )
        parts.extend(PostgresqlObjectGrants.get_parts(sequence_privilege_rows, GrantTarget.SEQUENCE))
        parts.extend(
            PostgresqlObjectGrants.get_parts(
                function_privilege_rows,
                GrantTarget.FUNCTION,
                function_signatures=function_signatures,
            )
        )
        return parts
