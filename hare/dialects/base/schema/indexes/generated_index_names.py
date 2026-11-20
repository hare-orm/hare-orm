from __future__ import annotations

from collections.abc import Mapping
from copy import copy
from typing import Any

from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.enums import GeneratedNamePrefix
from hare.ddl.generated_names import GeneratedNames
from hare.ddl.indexes.index import Index
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.plans.schema_statement import SchemaStatement
from hare.dialects.base.schema.plans.schema_step import SchemaStep
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.models.model_binding import ModelBinding


class GeneratedIndexNames(SchemaEditorPart):
    """The names hare generates for indexes and constraints from the table and the columns - found again
    and renamed when the table or a column is renamed."""

    __slots__ = ()

    def get_generated_index_names(
        self, binding: ModelBinding, field_names: Mapping[str, str] | None = None
    ) -> dict[tuple[str, Any], tuple[Index, bool]]:
        """Every index or unique constraint of `model` whose name is generated from its table and columns.

        Args:
            binding: The model.
            field_names: The name each field of `model` is keyed by - a renamed field keyed by its new
                name; its own name when missing.

        Returns:
            A key stable across a table or a field rename -> (an Index carrying the generated name,
            whether it backs a unique constraint rather than being a plain index).
        """
        field_names = field_names or {}
        generated: dict[tuple[str, Any], tuple[Index, bool]] = {}
        for field_name, field_column_names in binding.get_field_index_columns():
            generated[("field", field_names.get(field_name, field_name))] = (
                Index(
                    fields=(field_name,),
                    name=GeneratedNames.get_index_name(
                        GeneratedNamePrefix.INDEX, binding.db_table, field_column_names
                    ),
                ),
                False,
            )
        for position, entry in enumerate(binding.indexes or ()):
            declared_index = Index.from_declaration(entry)
            if declared_index.name:
                continue
            named_index = copy(declared_index)
            named_index.name = self.editor.index_statements.index_name_for_model(binding, declared_index)
            generated[("index", position)] = (named_index, False)
        for position, constraint in enumerate(binding.constraints or ()):
            if not isinstance(constraint, UniqueConstraint) or constraint.name:
                continue
            generated[("constraint", position)] = (
                self.get_unique_index(binding, tuple(constraint.fields), binding.get_column_names(constraint.fields)),
                True,
            )
        for field_name, column in binding.iter_field_unique_columns():
            generated[("unique", field_names.get(field_name, field_name))] = (
                self.get_unique_index(binding, (field_name,), (column,)),
                True,
            )
        return generated

    def get_unique_index(self, binding: ModelBinding, field_names: tuple[str, ...], column_names: Any) -> Index:
        """The unique index behind an unnamed unique constraint, under the constraint's generated name.

        Args:
            binding: The model.
            field_names: The constraint's fields.
            column_names: Their columns.

        Returns:
            The index.
        """
        return Index(
            fields=field_names,
            unique=True,
            name=GeneratedNames.get_index_name(GeneratedNamePrefix.UNIQUE_CONSTRAINT, binding.db_table, column_names),
        )

    def get_generated_constraint_names(
        self, binding: ModelBinding, field_names: Mapping[str, str] | None = None
    ) -> dict[tuple[str, ...], str]:
        """The generated names of the constraints of `model` with no index of their own to rename - its
        primary key and its foreign keys.

        Args:
            binding: The model.
            field_names: The name each field of `model` is keyed by - a renamed field keyed by its new
                name; its own name when missing.

        Returns:
            A key stable across a table or a field rename -> the constraint's generated name.
        """
        field_names = field_names or {}
        names: dict[tuple[str, ...], str] = {}
        if binding.has_primary_key:
            primary_key_columns = (
                [binding.fields_db_projection[name] for name in binding.primary_key_attribute]
                if binding.has_composite_primary_key
                else [binding.db_pk_column]
            )
            names[("primary_key",)] = self.editor.column_definitions.get_primary_key_name(binding, primary_key_columns)
        for field_name, field in binding.fields_map.items():
            if not isinstance(field, ForeignKeyFieldInstance):
                continue
            if not self.editor.foreign_key_rebuild.creates_foreign_key(field):
                continue
            # A relation names its key column(s).
            names[("foreign_key", field_names.get(field_name, field_name))] = GeneratedNames.get_foreign_key_name(
                binding.db_table, binding.get_column_names((field_name,))
            )
        return names

    def get_generated_index_names_change_plan(
        self,
        old_binding: ModelBinding,
        new_binding: ModelBinding,
        renamed_field_names: Mapping[str, str] | None = None,
        *,
        after_column_alter: bool = False,
        rebuilt_foreign_key: str | None = None,
    ) -> SchemaChangePlan:
        """Prepares renaming indexes and constraints after their table or columns, so
        they keep the names a later migration computes for them.

        Args:
            old_binding: The binding of the model under its old table and column names.
            new_binding: The binding of the model under the new names, used when the plan executes.
            renamed_field_names: The new name of each field of `old_model` the rename gave another name.
            after_column_alter: The column plan has already handled its field-owned indexes and uniqueness.
            rebuilt_foreign_key: A relation whose constraint was replaced, under its new name.
        """
        steps: list[SchemaStep] = []
        new_generated = self.get_generated_index_names(new_binding)
        old_generated = self.get_generated_index_names(old_binding, renamed_field_names)
        # A field's own index can be listed twice - by the field and as a Meta.indexes entry.
        renamed_names = (
            {
                index.name
                for key, (index, _) in old_generated.items()
                if key[0] in {"field", "unique"} and key in new_generated
            }
            if after_column_alter
            else set()
        )
        for key, (old_index, is_constraint) in old_generated.items():
            new_entry = new_generated.get(key)
            if new_entry is None or new_entry[0].name == old_index.name or old_index.name in renamed_names:
                continue
            renamed_names.add(old_index.name)
            steps.extend(
                self.get_generated_index_rename_plan(new_binding, old_index, new_entry[0], is_constraint).steps
            )
        if self.editor.RENAME_CONSTRAINT_TEMPLATE is None:
            # The constraints keep the names they were made with where the database renames none.
            return SchemaChangePlan(tuple(steps))
        new_constraint_names = self.get_generated_constraint_names(new_binding)
        for key, old_name in self.get_generated_constraint_names(old_binding, renamed_field_names).items():
            new_name = new_constraint_names.get(key)
            if new_name is None or new_name == old_name or key == ("foreign_key", rebuilt_foreign_key):
                continue
            steps.append(
                SchemaStatement(
                    self.editor.RENAME_CONSTRAINT_TEMPLATE.format(
                        table=self.editor.qualify_table_name(new_binding.db_table, new_binding.schema),
                        old_name=self.editor.quote(old_name),
                        new_name=self.editor.quote(new_name),
                    )
                )
            )
        return SchemaChangePlan(tuple(steps))

    def get_generated_index_rename_plan(
        self, binding: ModelBinding, old_index: Index, new_index: Index, is_constraint: bool
    ) -> SchemaChangePlan:
        """Prepares renaming one generated index, leaving a missing one alone where supported.

        Args:
            binding: The model under its new table name.
            old_index: The index under its old generated name.
            new_index: The index under its new generated name.
            is_constraint: Whether the index backs a unique constraint.
        """
        if self.editor.RENAME_INDEX_IF_EXISTS_TEMPLATE is None:
            return self.editor.get_rename_index_plan(binding, old_index, new_index)
        return SchemaChangePlan(
            (
                SchemaStatement(
                    self.editor.RENAME_INDEX_IF_EXISTS_TEMPLATE.format(
                        old_name=self.editor.qualify_table_name(old_index.name, binding.schema),  # type: ignore[arg-type]
                        new_name=self.editor.quote(new_index.name),  # type: ignore[arg-type]
                    )
                ),
            )
        )
