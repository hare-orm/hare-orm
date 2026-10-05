from __future__ import annotations

from copy import copy
from typing import Any, cast

from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.enums import GeneratedNamePrefix
from hare.ddl.generated_names import GeneratedNames
from hare.ddl.indexes.index import Index
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.models import Model


class GeneratedIndexNames(SchemaEditorPart):
    """The names hare generates for indexes from the table and the columns - found again and renamed
    when the table is renamed."""

    __slots__ = ()

    def get_generated_index_names(self, model: type[Model]) -> dict[tuple[str, Any], tuple[Index, bool]]:
        """Every index or unique constraint of `model` whose name is generated from its table.

        Args:
            model: The model.

        Returns:
            A key stable across a table rename -> (an Index carrying the generated name, whether
            it backs a unique constraint rather than being a plain index).
        """
        generated: dict[tuple[str, Any], tuple[Index, bool]] = {}
        for field_name, field_column_names in model._meta.get_field_index_columns():
            generated[("field", field_name)] = (
                Index(
                    fields=(field_name,),
                    name=GeneratedNames.get_index_name(GeneratedNamePrefix.INDEX, model, field_column_names),
                ),
                False,
            )
        for position, entry in enumerate(model._meta.indexes or ()):
            declared_index = entry if isinstance(entry, Index) else Index(fields=tuple(entry))
            if declared_index.name:
                continue
            named_index = copy(declared_index)
            named_index.name = self.editor.index_statements.index_name_for_model(model, declared_index)
            generated[("index", position)] = (named_index, False)
        for position, constraint in enumerate(model._meta.constraints or ()):
            if not isinstance(constraint, UniqueConstraint) or constraint.name:
                continue
            column_names = model._meta.get_column_names(constraint.fields)
            generated[("constraint", position)] = (
                Index(
                    fields=tuple(constraint.fields),
                    unique=True,
                    name=self.editor.constraint_names.constraint_name_for_model(
                        model, UniqueConstraint(fields=tuple(column_names))
                    ),
                ),
                True,
            )
        return generated

    async def rename_generated_index_names(self, old_model: type[Model], new_model: type[Model]) -> None:
        """Renames the indexes and unique constraints named after a table that was just renamed,
        so they keep the names a later migration computes for them.

        Args:
            old_model: The model under its old table name.
            new_model: The model under its new table name - its table already renamed.
        """
        new_generated = self.get_generated_index_names(new_model)
        for key, (old_index, is_constraint) in self.get_generated_index_names(old_model).items():
            new_entry = new_generated.get(key)
            if new_entry is None or new_entry[0].name == old_index.name:
                continue
            await self.rename_generated_index(new_model, old_index, new_entry[0], is_constraint)

    async def rename_generated_index(
        self, model: type[Model], old_index: Index, new_index: Index, is_constraint: bool
    ) -> None:
        """Renames one index with a generated name; a missing one is left alone.

        Args:
            model: The model under its new table name.
            old_index: The index under its old generated name.
            new_index: The index under its new generated name.
            is_constraint: Whether the index backs a unique constraint.
        """
        if self.editor.RENAME_INDEX_IF_EXISTS_TEMPLATE is None:
            await self.editor.rename_index(model, old_index, new_index)
            return
        await self.editor.run_sql(
            self.editor.RENAME_INDEX_IF_EXISTS_TEMPLATE.format(
                old_name=self.editor.qualify_table_name(cast("str", old_index.name), model._meta.schema),
                new_name=self.editor.quote(cast("str", new_index.name)),
            )
        )
