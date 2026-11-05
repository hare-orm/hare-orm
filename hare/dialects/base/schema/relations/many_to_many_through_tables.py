from __future__ import annotations

import inspect
from copy import copy

from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.enums import GeneratedNamePrefix
from hare.ddl.generated_names import GeneratedNames
from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.plans.schema_statement import SchemaStatement
from hare.dialects.base.schema.plans.schema_step import SchemaStep
from hare.dialects.base.schema.plans.through_table_copy import ThroughTableCopy
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.exceptions import ConfigurationError
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.models import Model


class ManyToManyThroughTables(SchemaEditorPart):
    """The through tables of many-to-many relations when a relation changes: the table altered or
    replaced, its indexes kept, its rows moved between an automatic and a declared through model."""

    __slots__ = ()

    def get_many_to_many_change_plan(
        self,
        model: type[Model],
        old_field: ManyToManyFieldInstance[Model],
        new_field: ManyToManyFieldInstance[Model],
    ) -> SchemaChangePlan:
        """Compile changes to automatic relation storage and its constraints together.

        Changing between automatic and declared through models moves their rows. Between
        two declared through models, their model operations perform the changes instead.

        Args:
            model: The model declaring the relation.
            old_field: The relation as declared before.
            new_field: The relation as declared now.
        """
        if old_field.through_model is not None and new_field.through_model is not None:
            return SchemaChangePlan(())
        if old_field.through_model is not None or new_field.through_model is not None:
            return self.get_through_table_move_plan(model, old_field, new_field)
        foreign_keys = self.editor.foreign_key_rebuild
        rebuilds_keys = self.editor.client.features.supports_foreign_keys and (
            foreign_keys.foreign_key_changed(old_field, new_field) or self.target_table_changed(old_field, new_field)
        )
        steps: list[SchemaStep] = []
        if rebuilds_keys:
            steps.extend(foreign_keys.get_relation_foreign_key_drop_plan(model, old_field).steps)
        schema = model._meta.schema
        if old_field.through != new_field.through:
            steps.append(
                SchemaStatement(self.editor.get_table_rename_sql(old_field.through, new_field.through, schema))
            )
        table = self.editor.get_schema_table_sql(new_field.through, schema)
        for old_keys, new_keys in (
            (old_field.forward_keys, new_field.forward_keys),
            (old_field.backward_keys, new_field.backward_keys),
        ):
            for old_key, new_key in zip(old_keys, new_keys, strict=True):
                if old_key != new_key:
                    steps.append(
                        SchemaStatement(
                            self.editor.RENAME_FIELD_TEMPLATE.format(
                                table=table,
                                old_column=self.editor.quote(old_key),
                                new_column=self.editor.quote(new_key),
                            )
                        )
                    )
        steps.extend(self.get_through_index_change_plan(schema, old_field, new_field).steps)
        if rebuilds_keys:
            steps.extend(foreign_keys.get_many_to_many_rebuild_plan(model, new_field).steps)
        else:
            steps.extend(self.get_through_foreign_key_rename_plan(table, old_field, new_field).steps)
        return SchemaChangePlan(tuple(steps))

    @staticmethod
    def target_table_changed(
        old_field: ManyToManyFieldInstance[Model], new_field: ManyToManyFieldInstance[Model]
    ) -> bool:
        """Whether a many-to-many relation points at another table than before - its foreign key to the
        target references the old one otherwise, and goes with it when that table is dropped.

        Args:
            old_field: The relation as declared before.
            new_field: The relation as declared now.
        """
        old_meta, new_meta = old_field.related_model._meta, new_field.related_model._meta
        return (old_meta.schema, old_meta.db_table) != (new_meta.schema, new_meta.db_table)

    def get_through_foreign_key_rename_plan(
        self,
        qualified_through: str,
        old_field: ManyToManyFieldInstance[Model],
        new_field: ManyToManyFieldInstance[Model],
    ) -> SchemaChangePlan:
        """Renames the foreign keys of an automatic through table renamed or with key columns renamed -
        a foreign key is named after its table and columns.

        Args:
            qualified_through: The through table, under its new name.
            old_field: The relation's previous definition.
            new_field: The relation's new definition.
        """
        if self.editor.RENAME_CONSTRAINT_TEMPLATE is None or not self.editor.foreign_key_rebuild.creates_foreign_key(
            new_field
        ):
            return SchemaChangePlan(())
        statements = []
        for old_keys, new_keys in (
            (old_field.backward_keys, new_field.backward_keys),
            (old_field.forward_keys, new_field.forward_keys),
        ):
            old_name = GeneratedNames.get_foreign_key_name(old_field.through, old_keys)
            new_name = GeneratedNames.get_foreign_key_name(new_field.through, new_keys)
            if old_name != new_name:
                statements.append(
                    self.editor.RENAME_CONSTRAINT_TEMPLATE.format(
                        table=qualified_through,
                        old_name=self.editor.quote(old_name),
                        new_name=self.editor.quote(new_name),
                    )
                )
        return SchemaChangePlan(tuple(SchemaStatement(sql) for sql in statements))

    def get_through_index_change_plan(
        self,
        schema: str | None,
        old_field: ManyToManyFieldInstance[Model],
        new_field: ManyToManyFieldInstance[Model],
    ) -> SchemaChangePlan:
        """Drops the automatic through table's indexes - its unique index over both sides and its plain
        key indexes - the old definition had and the new one doesn't, and creates the new ones - an index
        is named after its table and columns, so a renamed table or key column moves it too.

        Args:
            schema: The through table's schema.
            old_field: The relation's previous definition.
            new_field: The relation's new definition.
        """
        statements = []
        old_index_names = set(self.get_through_indexes(old_field))
        new_index_keys_by_name = self.get_through_indexes(new_field)
        for index_name in sorted(old_index_names - new_index_keys_by_name.keys()):
            statements.append(
                self.editor.index_statements.get_table_index_drop_sql(new_field.through, index_name, schema)
            )
        for index_name, (index_keys, unique) in new_index_keys_by_name.items():
            if index_name in old_index_names:
                continue
            index_statements = self.editor.index_statements
            statements.append(
                index_statements.get_unique_index_sql(new_field.through, index_keys, schema=schema)
                if unique
                else index_statements.get_table_index_sql(new_field.through, index_keys, schema=schema)
            )
        return SchemaChangePlan(tuple(SchemaStatement(sql) for sql in statements))

    def get_through_indexes(self, field: ManyToManyFieldInstance[Model]) -> dict[str, tuple[tuple[str, ...], bool]]:
        """The indexes of a relation's automatic through table, by name.

        Args:
            field: The relation.

        Returns:
            The name -> (the indexed columns, whether the index is unique).
        """
        indexes: dict[str, tuple[tuple[str, ...], bool]] = {}
        if field.unique and self.editor.client.features.supports_unique_constraints:
            unique_keys = (*field.backward_keys, *field.forward_keys)
            indexes[GeneratedNames.get_index_name(GeneratedNamePrefix.UNIQUE_INDEX, field.through, unique_keys)] = (
                unique_keys,
                True,
            )
        for index_keys in field.get_through_index_keys():
            indexes[GeneratedNames.get_index_name(GeneratedNamePrefix.INDEX, field.through, index_keys)] = (
                tuple(index_keys),
                False,
            )
        return indexes

    def get_through_table_drop_plan(
        self, model: type[Model], field: ManyToManyFieldInstance[Model]
    ) -> SchemaChangePlan:
        """Prepare removal only of an automatic through table declared on this side."""
        if field.through_model is not None or field._generated:
            return SchemaChangePlan(())
        return SchemaChangePlan(
            (
                SchemaStatement(
                    self.editor.DELETE_TABLE_TEMPLATE.format(
                        table=self.editor.get_schema_table_sql(
                            field.through, field.through_schema or model._meta.schema
                        ),
                    )
                ),
            )
        )

    def get_model_through_drop_plan(self, model: type[Model]) -> SchemaChangePlan:
        """Prepare removal of every automatic through table declared by the model."""
        steps: list[SchemaStep] = []
        for field_name in sorted(model._meta.many_to_many_fields):
            field = model._meta.fields_map[field_name]
            if isinstance(field, ManyToManyFieldInstance):
                steps.extend(self.get_through_table_drop_plan(model, field).steps)
        return SchemaChangePlan(tuple(steps))

    def get_through_table_move_plan(
        self,
        model: type[Model],
        old_field: ManyToManyFieldInstance[Model],
        new_field: ManyToManyFieldInstance[Model],
    ) -> SchemaChangePlan:
        """Compile moving relation rows, deferring only callable defaults and generated keys.

        Constant defaults and unsupported unique callable defaults are checked before
        creation of an automatic destination table. Existing declared through tables
        are never dropped by this plan.
        """
        steps: list[SchemaStep] = []
        if new_field.through_model is None:
            definition = self.editor.table_creation.get_many_to_many_table_definition(model, new_field)
            if definition:
                steps.append(SchemaStatement(definition))
        source = self.editor.qualify_table_name(old_field.through, old_field.through_schema or model._meta.schema)
        target = self.editor.qualify_table_name(new_field.through, new_field.through_schema or model._meta.schema)
        columns = [*new_field.backward_keys, *new_field.forward_keys]
        values = [self.editor.quote(key) for key in (*old_field.backward_keys, *old_field.forward_keys)]
        through_model = new_field.through_model_class
        defaults = []
        generated_key_column = None
        if through_model is not None:
            unique_fields = {
                name
                for constraint in through_model._meta.constraints
                if isinstance(constraint, UniqueConstraint)
                for name in constraint.fields
            }
            backfill = self.editor.column_backfill
            for name, column in through_model._meta.fields_db_projection.items():
                field = through_model._meta.fields_map[name]
                if column in columns or field.pk or field.generated or field.has_db_default():
                    continue
                if field.default is None and not backfill.is_auto_now_field(field):
                    continue
                runtime_default = callable(field.default) or inspect.isawaitable(field.default)
                if runtime_default and (field.unique or name in unique_fields):
                    raise ConfigurationError(
                        f"Can't carry the rows of {new_field.model_field_name!r} over into "
                        f"{through_model.__name__}'s table - its unique field {name!r} has a callable "
                        "default, and computing it once would give every row the same value. Give it a "
                        "db_default, or fill the through table yourself in a RunPython step."
                    )
                if runtime_default or (field.default is None and backfill.is_auto_now_field(field)):
                    defaults.append((column, copy(field)))
                else:
                    columns.append(column)
                    values.append(backfill.backfill_default_sql_literal(field, field.default, through_model))
            key = through_model._meta.pk
            if key is not None and key.generated:
                key_column = through_model._meta.fields_db_projection[key.model_field_name]
                if key_column not in columns:
                    generated_key_column = key_column
        step = ThroughTableCopy(
            source,
            target,
            tuple(columns),
            tuple(values),
            new_field.through_model is None,
            through_model,
            tuple(defaults),
            generated_key_column,
        )
        if defaults or generated_key_column is not None:
            steps.append(step)
        else:
            steps.append(
                SchemaStatement(step.get_sql(self.editor, columns, values), frozenset({SchemaEffect.REWRITE_ROWS}))
            )
        if old_field.through_model is None:
            steps.append(
                SchemaStatement(
                    self.editor.DELETE_TABLE_TEMPLATE.format(
                        table=self.editor.get_schema_table_sql(
                            old_field.through, old_field.through_schema or model._meta.schema
                        )
                    )
                )
            )
        return SchemaChangePlan(tuple(steps))
