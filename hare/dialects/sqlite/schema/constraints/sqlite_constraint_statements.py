from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.dialects.base.schema.constraints.constraint_statements import ConstraintStatements
from hare.models import Model

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor


class SqliteConstraintStatements(ConstraintStatements):
    """ConstraintStatements as SQLite writes it."""

    __slots__ = ()

    editor: SqliteSchemaEditor

    async def add_unique_constraint_using_index(
        self, model: type[Model], constraint: UniqueConstraint, index_name: str
    ) -> None:
        # A SQLite unique constraint is a unique index of its own name - an index of that name
        # already is the constraint.
        if index_name == constraint.name:
            return
        await super().add_unique_constraint_using_index(model, constraint, index_name)
