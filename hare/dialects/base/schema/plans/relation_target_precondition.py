from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.schema.constants import RELATION_TARGET_CHANGE_MESSAGE, RELATION_TARGET_CHECK_PREVIEW
from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.migrations.exceptions import ForeignKeyTargetChangeError

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


@dataclass(frozen=True, slots=True)
class RelationTargetPrecondition:
    """Reject changing a relation's target while values still name the old target."""

    sql: str
    relation: str
    old_target: str
    new_target: str
    effects: ClassVar[frozenset[SchemaEffect]] = frozenset({SchemaEffect.CHECK_ROWS})

    async def execute(self, editor: BaseSchemaEditor) -> None:
        """Read stored keys only at execution, before any constraint is removed."""
        if editor.collect_sql:
            editor.collected_sql.append(RELATION_TARGET_CHECK_PREVIEW.format(relation=self.relation))
            return
        rows = await editor.client.execute_dicts(self.sql)
        count = rows[0]["stored_count"]
        if count:
            raise ForeignKeyTargetChangeError(
                RELATION_TARGET_CHANGE_MESSAGE.format(
                    relation=self.relation,
                    old=self.old_target,
                    new=self.new_target,
                    count=count,
                )
            )
