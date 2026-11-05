from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from hare.models.model import Model
    from hare.query.execution.write_statements import WriteStatement
    from hare.sql.builder.update_from_values import UpdateFromValues
    from hare.sql.sql_context import SqlContext

TModel = TypeVar("TModel", bound="Model")


@dataclasses.dataclass(frozen=True, slots=True)
class BoundUpdateBatch(Generic[TModel]):
    """One fully bound UPDATE and the input objects to which its result applies."""

    command: WriteStatement
    objects: list[TModel]


@dataclasses.dataclass(frozen=True, slots=True)
class BulkUpdateStatementLayout:
    """What every ``UPDATE ... FROM (VALUES ...)`` statement of one ``bulk_update()`` shares - each
    batch of objects only adds its rows.

    Attributes:
        write: The structural assignments, row positions, filter and returned columns.
        base_context: The dialect's base SQL context.
        row_cast_types: The SQL cast of each row field, None when no cast is needed.
        row_fields: The fields of a VALUES row - the key, the old version, the written fields.
        row_placeholder_templates: The placeholder template of each of them.
        batch_size: How many objects one statement writes.
    """

    write: UpdateFromValues
    base_context: SqlContext
    row_cast_types: list[str | None]
    row_fields: list[str]
    row_placeholder_templates: tuple[str, ...]
    batch_size: int | None
