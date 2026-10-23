from __future__ import annotations

from collections.abc import Mapping
from copy import copy
from typing import Any, cast

from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.enums import GeneratedNamePrefix
from hare.ddl.generated_names import GeneratedNames
from hare.ddl.indexes.index import Index
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.models import Model


class GeneratedIndexNames(SchemaEditorPart):
    """The names hare generates for indexes and constraints from the table and the columns - found again
    and renamed when the table or a column is renamed."""

    __slots__ = ()

    def get_generated_index_names(
        self, model: type[Model], field_names: Mapping[str, str] | None = None
    ) -> dict[tuple[str, Any], tuple[Index, bool]]:
        """Every index or unique constraint of `model` whose name is generated from its table and columns.

        Args:
            model: The model.
            field_names: The name each field of `model` is keyed by - a renamed field keyed by its new
                name; its own name when missing.

        Returns:
            A key stable across a table or a field rename -> (an Index carrying the generated name,
            whether it backs a unique constraint rather than being a plain index).
        """
        field_names = field_names or {}
        meta = model._meta
        generated: dict[tuple[str, Any], tuple[Index, bool]] = {}
        for field_name, field_column_names in meta.get_field_index_columns():
            generated[("field", field_names.get(field_name, field_name))] = (
                Index(
                    fields=(field_name,),
                    name=GeneratedNames.get_index_name(GeneratedNamePrefix.INDEX, model, field_column_names),
                ),
                False,
            )
        for position, entry in enumerate(meta.indexes or ()):
            declared_index = entry if isinstance(entry, Index) else Index(fields=tuple(entry))
            if declared_index.name:
                continue
            named_index = copy(declared_index)
            named_index.name = self.editor.index_statements.index_name_for_model(model, declared_index)
            generated[("index", position)] = (named_index, False)
        for position, constraint in enumerate(meta.constraints or ()):
            if not isinstance(constraint, UniqueConstraint) or constraint.name:
                continue
            generated[("constraint", position)] = (
                self.get_unique_index(model, tuple(constraint.fields), meta.get_column_names(constraint.fields)),
                True,
            )
        for field_name, column in meta.fields_db_projection.items():
            field = meta.fields_map[field_name]
            reference = getattr(field, "reference", None)
            if field.unique and not field.pk and not (reference is not None and len(reference.to_field_instances) > 1):
                generated[("unique", field_names.get(field_name, field_name))] = (
                    self.get_unique_index(model, (field_name,), (column,)),
                    True,
                )
        return generated

    def get_unique_index(self, model: type[Model], field_names: tuple[str, ...], column_names: Any) -> Index:
        """The unique index behind an unnamed unique constraint, under the constraint's generated name.

        Args:
            model: The model.
            field_names: The constraint's fields.
            column_names: Their columns.

        Returns:
            The index.
        """
        return Index(
            fields=field_names,
            unique=True,
            name=self.editor.constraint_names.constraint_name_for_model(
                model, UniqueConstraint(fields=tuple(column_names))
            ),
        )

    def get_generated_constraint_names(
        self, model: type[Model], field_names: Mapping[str, str] | None = None
    ) -> dict[tuple[str, ...], str]:
        """The generated names of the constraints of `model` with no index of their own to rename - its
        primary key and its foreign keys.

        Args:
            model: The model.
            field_names: The name each field of `model` is keyed by - a renamed field keyed by its new
                name; its own name when missing.

        Returns:
            A key stable across a table or a field rename -> the constraint's generated name.
        """
        field_names = field_names or {}
        meta = model._meta
        names: dict[tuple[str, ...], str] = {}
        if meta.has_primary_key:
            primary_key_columns = (
                [meta.fields_db_projection[name] for name in meta.primary_key_attribute]
                if meta.has_composite_primary_key
                else [meta.db_pk_column]
            )
            names[("primary_key",)] = self.editor.column_definitions.get_primary_key_name(model, primary_key_columns)
        for field_name, field in meta.fields_map.items():
            if not isinstance(field, ForeignKeyFieldInstance):
                continue
            if not self.editor.foreign_key_rebuild.creates_foreign_key(field):
                continue
            # A relation names its key column(s).
            names[("foreign_key", field_names.get(field_name, field_name))] = GeneratedNames.get_foreign_key_name(
                meta.db_table, meta.get_column_names((field_name,))
            )
        return names

    async def rename_generated_index_names(
        self, old_model: type[Model], new_model: type[Model], renamed_field_names: Mapping[str, str] | None = None
    ) -> None:
        """Renames the indexes and constraints named after a table or columns that were just renamed, so
        they keep the names a later migration computes for them.

        Args:
            old_model: The model under its old table and column names.
            new_model: The model under the new ones - its table and columns already renamed.
            renamed_field_names: The new name of each field of `old_model` the rename gave another name.
        """
        new_generated = self.get_generated_index_names(new_model)
        # A field's own index can be listed twice - by the field and as a Meta.indexes entry.
        renamed_names: set[str | None] = set()
        for key, (old_index, is_constraint) in self.get_generated_index_names(old_model, renamed_field_names).items():
            new_entry = new_generated.get(key)
            if new_entry is None or new_entry[0].name == old_index.name or old_index.name in renamed_names:
                continue
            renamed_names.add(old_index.name)
            await self.rename_generated_index(new_model, old_index, new_entry[0], is_constraint)
        if self.editor.RENAME_CONSTRAINT_TEMPLATE is None:
            # The constraints keep the names they were made with where the database renames none.
            return
        new_constraint_names = self.get_generated_constraint_names(new_model)
        for key, old_name in self.get_generated_constraint_names(old_model, renamed_field_names).items():
            new_name = new_constraint_names.get(key)
            if new_name is None or new_name == old_name:
                continue
            await self.editor.run_sql(
                self.editor.RENAME_CONSTRAINT_TEMPLATE.format(
                    table=self.editor.qualify_table_name(new_model._meta.db_table, new_model._meta.schema),
                    old_name=self.editor.quote(old_name),
                    new_name=self.editor.quote(new_name),
                )
            )

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
