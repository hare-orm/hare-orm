from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.dialects.base.schema.indexes.generated_index_names import GeneratedIndexNames
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.plans.schema_statement import SchemaStatement
from hare.dialects.sqlite.indexes.separate_table_index import SeparateTableIndex
from hare.models.enums import ModelOption

if TYPE_CHECKING:
    from hare.ddl.indexes.index import Index
    from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor
    from hare.models.model import Model


class SqliteGeneratedIndexNames(GeneratedIndexNames):
    """GeneratedIndexNames as SQLite writes it."""

    __slots__ = ()

    editor: SqliteSchemaEditor

    def get_generated_index_rename_plan(
        self, model: type[Model], old_index: Index, new_index: Index, is_constraint: bool
    ) -> SchemaChangePlan:
        """SQLite has no ALTER INDEX ... RENAME - the index, a unique constraint's too, is dropped and
        created again under its new name. An index kept in a table of its own is created again by
        rename_table()."""
        if isinstance(old_index, SeparateTableIndex) or old_index.name is None or new_index.name is None:
            return SchemaChangePlan(())
        drop = SchemaStatement(
            self.editor.DROP_INDEX_TEMPLATE.format(
                name=self.editor.qualify_table_name(old_index.name, model._meta.schema)
            )
        )
        partial_unique_constraint = self.get_partial_unique_constraint_named(model, new_index.name)
        if is_constraint and partial_unique_constraint is not None:
            create = self.editor.get_add_constraint_plan(model, partial_unique_constraint)
        else:
            create = self.editor.get_add_index_plan(model, new_index)
        return SchemaChangePlan((drop, *create.steps))

    def get_partial_unique_constraint_named(self, model: type[Model], index_name: str) -> UniqueConstraint | None:
        """The model's partial unique constraint whose index has the given name.

        Args:
            model: The model.
            index_name: The name of the constraint's index.

        Returns:
            The ``condition=`` UniqueConstraint, or None when no such constraint has that name.
        """
        for constraint in getattr(model._meta, ModelOption.CONSTRAINTS, None) or ():
            if not isinstance(constraint, UniqueConstraint) or not constraint.condition:
                continue
            if self.editor.constraint_names.constraint_name_for_model(model, constraint) == index_name:
                return constraint
        return None
