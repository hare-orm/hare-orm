from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.schema.schema_objects.extensions import Extensions

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlExtensions(Extensions):
    """Extensions as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    def get_extension_create_sql(self, extension: str) -> str:
        return f"CREATE EXTENSION IF NOT EXISTS {self.editor.quote(extension)};"

    async def create_extension(self, extension_name: str) -> None:
        await self.editor.run_sql(self.get_extension_create_sql(extension_name))

    async def drop_extension(self, extension_name: str) -> None:
        await self.editor.run_sql(f"DROP EXTENSION IF EXISTS {self.editor.quote(extension_name)};")

    async def create_collation(self, name: str, locale: str, provider: str, deterministic: bool) -> None:
        locale_literal = "'" + locale.replace("'", "''") + "'"
        options = [f"locale = {locale_literal}", f"provider = {provider}"]
        if not deterministic:
            options.append("deterministic = false")
        await self.editor.run_sql(f"CREATE COLLATION {self.editor.quote(name)} ({', '.join(options)});")

    async def drop_collation(self, name: str) -> None:
        await self.editor.run_sql(f"DROP COLLATION {self.editor.quote(name)};")
