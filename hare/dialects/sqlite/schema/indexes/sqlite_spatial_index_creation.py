from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.dialects.sqlite.constants import SPATIALITE_INDEX_RESULT_COLUMN
from hare.dialects.sqlite.schema.constants import SPATIALITE_CREATION_FAILED, SPATIALITE_CREATION_FAILURE_REASONS
from hare.exceptions import OperationalError

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


@dataclass(frozen=True, slots=True)
class SqliteSpatialIndexCreation:
    """Prepared registration and index creation whose SQL result reports acceptance."""

    statements: tuple[str, ...]
    description: str
    effects: ClassVar[frozenset[SchemaEffect]] = frozenset({SchemaEffect.CHECK_ROWS})

    async def execute(self, editor: BaseSchemaEditor) -> None:
        """Preserve the extension's explicit failure signal rather than ignoring its rows."""
        for position, sql in enumerate(self.statements):
            if editor.collect_sql:
                await editor.run_sql(sql)
                continue
            editor.run_statement_count += 1
            rows = await editor.client.execute_dicts(sql.removesuffix(";"))
            if not rows or rows[0][SPATIALITE_INDEX_RESULT_COLUMN] != 1:
                raise OperationalError(
                    SPATIALITE_CREATION_FAILED.format(
                        description=self.description,
                        reason=SPATIALITE_CREATION_FAILURE_REASONS[min(position, 1)],
                    )
                )
