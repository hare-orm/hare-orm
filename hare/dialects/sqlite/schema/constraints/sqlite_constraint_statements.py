from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.dialects.base.schema.constraints.constraint_statements import ConstraintStatements
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.models import Model

if TYPE_CHECKING:
    from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor


class SqliteConstraintStatements(ConstraintStatements):
    """ConstraintStatements as SQLite writes it."""

    __slots__ = ()

    editor: SqliteSchemaEditor

    def get_unique_constraint_clause(self, model: type[Model], constraint: UniqueConstraint) -> str | None:
        # A SQLite table takes no constraint after it is created - every unique constraint is a unique
        # index of its name.
        return None

    def get_unique_constraint_using_index_plan(
        self, model: type[Model], constraint: UniqueConstraint, index_name: str
    ) -> SchemaChangePlan:
        # A SQLite unique constraint is a unique index of its own name - an index of that name
        # already is the constraint.
        if index_name == constraint.name:
            return SchemaChangePlan(())
        return super().get_unique_constraint_using_index_plan(model, constraint, index_name)
