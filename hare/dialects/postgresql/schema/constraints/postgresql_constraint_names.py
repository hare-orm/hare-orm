from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.schema.constraints.constraint_names import ConstraintNames

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlConstraintNames(ConstraintNames):
    """ConstraintNames as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    async def get_unique_constraint_names_from_db(
        self, table_name: str, column_names: list[str], schema: str | None = None
    ) -> list[str]:
        """Query pg_constraint for unique constraint names matching exact column set."""
        # The names are bound as parameters, not put into the SQL text.
        query = (
            "SELECT con.conname "
            "FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace "
            "WHERE rel.relname = $1 "  # nosec B608
            "AND con.contype = 'u' "
            "AND nsp.nspname = COALESCE($2::text, current_schema()) "
            "AND ARRAY("
            "  SELECT att.attname::text"
            "  FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)"
            "  JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = k.attnum"
            "  ORDER BY k.ord"
            ") = $3::text[]"
        )
        _, rows = await self.editor.client.execute(query, [table_name, schema, column_names])
        return [row["conname"] for row in rows]
