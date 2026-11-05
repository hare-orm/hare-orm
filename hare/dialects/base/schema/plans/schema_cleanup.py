from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hare.core.resource_completion import ResourceCompletion
from hare.dialects.base.schema.constants import SCHEMA_CLEANUP_FAILED, SCHEMA_FAILURE_CLEANUP_PREVIEW
from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


@dataclass(frozen=True, slots=True)
class SchemaCleanup:
    """Prepared work whose cleanup must finish before control leaves the step.

    Resource acquisition belongs before this step: a failed CREATE must not drop
    an unrelated existing object. Failure and cancellation always run cleanup;
    ``release_on_success=False`` retains a successfully prepared resource for the
    following steps. Both errors are retained if work and release fail. Preview
    renders conditional cleanup as comments, not unconditional executable SQL.
    """

    body: SchemaChangePlan
    cleanup: SchemaChangePlan
    release_on_success: bool = True

    @property
    def effects(self) -> frozenset[SchemaEffect]:
        """Include the work of both the operation and its release."""
        return self.body.effects | self.cleanup.effects

    async def execute(self, editor: BaseSchemaEditor) -> None:
        """Drain cleanup even when cancellation interrupts its waiter repeatedly."""
        if editor.collect_sql:
            await self.body.execute(editor)
            start = len(editor.collected_sql)
            await self.cleanup.execute(editor)
            if not self.release_on_success:
                collected = editor.collected_sql[start:]
                editor.collected_sql[start:] = [
                    SCHEMA_FAILURE_CLEANUP_PREVIEW,
                    *(f"-- {line}" for statement in collected for line in statement.splitlines()),
                ]
            return
        try:
            await self.body.execute(editor)
        except BaseException as error:
            try:
                await ResourceCompletion.wait(self.cleanup.execute(editor))
            except BaseException as cleanup_error:
                raise BaseExceptionGroup(SCHEMA_CLEANUP_FAILED, [error, cleanup_error]) from None
            raise
        else:
            if self.release_on_success:
                await ResourceCompletion.wait(self.cleanup.execute(editor))
