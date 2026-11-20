from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from hare.core.resource_completion import ResourceCompletion
from hare.dialects.base.schema.constants import SCHEMA_CLEANUP_FAILED, SCHEMA_FAILURE_CLEANUP_PREVIEW
from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.plans.schema_statement import SchemaStatement

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


@dataclass(frozen=True, slots=True)
class SchemaCleanup:
    """Prepared work whose cleanup must finish before control leaves the step.

    Optional acquisition is drained before entering the body: a failed CREATE must
    not drop an unrelated existing object. Cancellation during a successful CREATE
    releases its confirmed resource without starting the body. Body failure and cancellation run cleanup;
    ``release_on_success=False`` retains a successfully prepared resource for the
    following steps. Both errors are retained if work and release fail. Preview
    renders conditional cleanup as comments, not unconditional executable SQL.
    """

    body: SchemaChangePlan
    cleanup: SchemaChangePlan
    release_on_success: bool = True
    acquisition: SchemaStatement | None = field(default=None, kw_only=True)

    @property
    def effects(self) -> frozenset[SchemaEffect]:
        """Include the work of both the operation and its release."""
        return (
            self.body.effects
            | self.cleanup.effects
            | (self.acquisition.effects if self.acquisition is not None else frozenset())
        )

    async def execute(self, editor: BaseSchemaEditor) -> None:
        """Drain cleanup even when cancellation interrupts its waiter repeatedly."""
        if editor.collect_sql:
            if self.acquisition is not None:
                await self.acquisition.execute(editor)
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
        if self.acquisition is not None:
            acquisition = asyncio.create_task(self.acquisition.execute(editor))
            try:
                await ResourceCompletion.wait(acquisition)
            except BaseException as error:
                if not acquisition.cancelled() and acquisition.exception() is None:
                    await self._release(editor, error)
                raise
        try:
            await self.body.execute(editor)
        except BaseException as error:
            await self._release(editor, error)
            raise
        else:
            if self.release_on_success:
                await self._release(editor)

    async def _release(self, editor: BaseSchemaEditor, error: BaseException | None = None) -> None:
        """Complete release while preserving both an operation error and a release error."""
        with ResourceCompletion.preserve_error(error, SCHEMA_CLEANUP_FAILED):
            await ResourceCompletion.wait(self.cleanup.execute(editor))
