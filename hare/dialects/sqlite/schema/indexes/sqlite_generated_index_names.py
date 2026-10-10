from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.dialects.base.schema.indexes.generated_index_names import GeneratedIndexNames
from hare.dialects.sqlite.indexes.own_table_index import OwnTableIndex
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.indexes.index import Index
    from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor
    from hare.models.model import Model


class SqliteGeneratedIndexNames(GeneratedIndexNames):
    """GeneratedIndexNames as SQLite writes it."""

    __slots__ = ()

    editor: SqliteSchemaEditor

    async def rename_generated_index(
        self, model: type[Model], old_index: Index, new_index: Index, is_constraint: bool
    ) -> None:
        """SQLite has no ALTER INDEX ... RENAME - a standalone index is dropped and re-created
        under its new name. A unique constraint inlined into CREATE TABLE is backed by an
        sqlite_autoindex_* that has no name of its own to change, so it is left as is. An index
        kept in a table of its own is created again by rename_table()."""
        if isinstance(old_index, OwnTableIndex) or old_index.name is None or new_index.name is None:
            return
        if not self.editor.collect_sql:
            rows = await self.editor.client.execute_dicts(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?", [old_index.name]
            )
            if not rows:
                return
        elif is_constraint:
            return
        await self.editor.run_sql(
            self.editor.DROP_INDEX_IF_EXISTS_TEMPLATE.format(name=self.editor.quote(old_index.name))
        )
        partial_unique_constraint = self.get_partial_unique_constraint_named(model, new_index.name)
        if is_constraint and partial_unique_constraint is not None:
            await self.editor.add_constraint(model, partial_unique_constraint)
            return
        await self.editor.add_index(model, new_index)

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
            column_constraint = UniqueConstraint(
                fields=tuple(self.editor.constraint_names.get_fields_to_columns(model, constraint.fields)),
                name=constraint.name,
            )
            if self.editor.constraint_names.constraint_name_for_model(model, column_constraint) == index_name:
                return constraint
        return None
