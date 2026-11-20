from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from hare.dialects.base.schema.plans.enums import SchemaEffect

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class SchemaStep(Protocol):
    """One prepared action shared by execution and SQL collection.

    A runtime action must collect a description of its required work when
    ``editor.collect_sql`` is true. It must neither connect nor run defaults in
    that mode. SQL-only actions collect their already prepared statements.
    """

    @property
    def effects(self) -> frozenset[SchemaEffect]:
        """The action's work, available without running it or guessing from SQL."""
        ...

    async def execute(self, editor: BaseSchemaEditor) -> None:
        """Execute on this editor, or collect the same action without I/O."""
        ...
