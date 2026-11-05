from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.dialects.sqlite.client.sqlite_pragma_scope import SqlitePragmaScope
from hare.dialects.sqlite.schema.constants import SQLITE_LEGACY_RENAME_PREVIEW

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


@dataclass(frozen=True, slots=True)
class SqliteLegacyRename:
    """Rename a replacement while preserving the connection's legacy-rename setting.

    The old setting is read only at execution. Restoration also follows a failed
    enabling statement, whose effect may already have reached the connection.
    Preview shows the rename and describes the runtime setting scope without IO.
    Legacy rename avoids revalidating views and triggers while the rebuilt table
    is briefly absent under its original name.
    """

    sql: str
    effects: ClassVar[frozenset[SchemaEffect]] = frozenset({SchemaEffect.READ_CATALOG})

    async def execute(self, editor: BaseSchemaEditor) -> None:
        """Temporarily enable legacy rename, then restore the exact boolean state."""
        if editor.collect_sql:
            editor.collected_sql.append(SQLITE_LEGACY_RENAME_PREVIEW)
            await editor.run_sql(self.sql)
            return
        async with SqlitePragmaScope.run(editor.client, "legacy_alter_table", "ON"):
            await editor.run_sql(self.sql)
