"""Applies migration operations in a test the way migrate does - through a Migration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hare.migrations.migration import Migration

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.operations.operation import Operation
    from hare.migrations.state.state import State


async def apply_operations(
    app_label: str,
    state: State,
    *operations: Operation,
    schema_editor: BaseSchemaEditor | None = None,
    dry_run: bool = False,
) -> State:
    """Applies operations to a state, and to the database of a schema editor.

    Args:
        app_label: The app the operations belong to.
        state: The state, changed in place.
        operations: The operations, in order.
        schema_editor: The editor of the database changed; None changes none.
        dry_run: Change the state alone.

    Returns:
        The state.
    """
    return await Migration("test", app_label, operations=list(operations)).apply(
        state, dry_run=dry_run, schema_editor=schema_editor
    )
