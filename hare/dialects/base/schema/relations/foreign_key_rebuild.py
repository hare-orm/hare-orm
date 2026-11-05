from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from hare.ddl.constraints.foreign_key_constraint import ForeignKeyConstraint
from hare.ddl.enums import GeneratedNamePrefix
from hare.ddl.generated_names import GeneratedNames
from hare.ddl.indexes.index import Index
from hare.dialects.base.schema.data.referencing_key_columns import ReferencingKeyColumns
from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.dialects.base.schema.plans.relation_target_precondition import RelationTargetPrecondition
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.plans.schema_statement import SchemaStatement
from hare.dialects.base.schema.plans.schema_step import SchemaStep
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.exceptions import UnSupportedError
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.models import Model


class ForeignKeyRebuild(SchemaEditorPart):
    """The foreign keys of the tables referencing a field that changes: dropped before the change,
    restored or rebuilt after it, with their on_delete and the referencing key columns found."""

    __slots__ = ()

    def get_referenced_field_change_plan(
        self,
        model: type[Model],
        old_field: Field[Any],
        new_field: Field[Any],
        referencing_key_columns: list[ReferencingKeyColumns],
    ) -> SchemaChangePlan:
        """Prepare both sides of a key-type change and all affected indexes."""
        steps: list[SchemaStep] = [
            step
            for referencing in referencing_key_columns
            for step in self.get_relation_foreign_key_drop_plan(referencing.model, referencing.relation_field).steps
        ]
        steps.extend(self.editor.get_alter_column_plan(model, old_field, new_field).steps)
        altered = []
        for referencing in referencing_key_columns:
            rebuild = self.editor.table_rebuild.get_referencing_key_rebuild_plan(referencing)
            if rebuild is None:
                altered.append(referencing)
            else:
                steps.extend(rebuild.steps)
        drops, restores = self.get_referencing_index_plans(altered)
        steps.extend(drops.steps)
        dialect = self.editor.client.dialect
        effects = (
            frozenset({SchemaEffect.REWRITE_ROWS})
            if self.editor.column_type_changes.column_type_change_rewrites_table(
                old_field.get_column_type(dialect), new_field.get_column_type(dialect)
            )
            else frozenset()
        )
        for referencing in altered:
            for column, sql_type in referencing.key_columns:
                changes = self.editor.column_type_changes.get_type_change_sql(
                    old_field, new_field, self.editor.quote(column), sql_type
                )
                steps.append(
                    SchemaStatement(
                        self.editor.ALTER_FIELD_TEMPLATE.format(table=referencing.qualified_table, changes=changes),
                        effects,
                    )
                )
        steps.extend(restores.steps)
        steps.extend(
            step
            for referencing in referencing_key_columns
            for step in self.get_relation_foreign_key_rebuild_plan(referencing.model, referencing.relation_field).steps
        )
        return SchemaChangePlan(tuple(steps))

    def get_referencing_index_plans(
        self, references: list[ReferencingKeyColumns]
    ) -> tuple[SchemaChangePlan, SchemaChangePlan]:
        """Compile temporary index removal and restoration around key-column changes."""
        if self.editor.client.features.alters_indexed_columns:
            return SchemaChangePlan(()), SchemaChangePlan(())
        drops: list[SchemaStep] = []
        restores: list[SchemaStep] = []
        index_statements = self.editor.index_statements
        seen = set()
        for reference in references:
            field = reference.relation_field
            model = reference.model
            schema = model._meta.schema
            if isinstance(field, ManyToManyFieldInstance):
                if field.through_model_class is not None:
                    continue
                for columns in field.get_through_index_keys():
                    name = GeneratedNames.get_index_name(GeneratedNamePrefix.INDEX, field.through, columns)
                    identity = (schema, field.through, name)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    drops.append(
                        SchemaStatement(
                            self.editor.DROP_INDEX_TEMPLATE.format(
                                name=self.editor.qualify_table_name(name, schema),
                                table=self.editor.qualify_table_name(field.through, schema),
                            )
                        )
                    )
                    restores.append(
                        SchemaStatement(index_statements.get_table_index_sql(field.through, columns, schema=schema))
                    )
                continue
            if not isinstance(field, ForeignKeyFieldInstance):
                continue
            composite = len(field.source_fields) > 1
            indexes = [Index(fields=(field.model_field_name,))] if composite and field.index else []
            indexes.extend(index_statements.get_indexes_covering_field(model, field, field.index and not composite))
            for index in indexes:
                identity = (schema, model._meta.db_table, index_statements.index_name_for_model(model, index))
                if identity in seen:
                    continue
                seen.add(identity)
                drops.append(SchemaStatement(index_statements.get_index_drop_sql(model, index)))
                restores.extend(self.editor.get_add_index_plan(model, index).steps)
        return SchemaChangePlan(tuple(drops)), SchemaChangePlan(tuple(restores))

    def get_relation_foreign_key_drop_plan(self, model: type[Model], relation_field: Field[Any]) -> SchemaChangePlan:
        """Prepare removal where foreign keys are separate schema objects."""
        return SchemaChangePlan(())

    def get_composite_index_drop_plan(
        self,
        model: type[Model],
        old_field: ForeignKeyFieldInstance[Model],
        key_field_pairs: list[tuple[Field[Any], Field[Any]]],
    ) -> SchemaChangePlan:
        """Drops the one index over all key columns of a relation to a composite primary key
        before a key column's type or nullability changes, on a database changing no indexed column
        (``Features.alters_indexed_columns``).

        Args:
            model: The model rendered from the target state.
            old_field: The relation's previous definition.
            key_field_pairs: Each key column's field before and after the change.

        Returns:
            The removal plan; its nonempty steps tell the restoration plan to recreate the index.
        """
        if (
            self.editor.client.features.alters_indexed_columns
            or len(old_field.source_fields) < 2
            or not old_field.index
        ):
            return SchemaChangePlan(())
        dialect = self.editor.client.dialect
        if not any(
            old_key_field.null != new_key_field.null
            or old_key_field.get_column_type(dialect) != new_key_field.get_column_type(dialect)
            for old_key_field, new_key_field in key_field_pairs
        ):
            return SchemaChangePlan(())
        return SchemaChangePlan(
            (
                SchemaStatement(
                    self.editor.index_statements.get_index_drop_sql(
                        model,
                        Index(fields=(old_field.model_field_name,)),
                    )
                ),
            )
        )

    def get_composite_index_change_plan(
        self,
        model: type[Model],
        old_field: ForeignKeyFieldInstance[Model],
        new_field: ForeignKeyFieldInstance[Model],
        *,
        index_dropped: bool = False,
    ) -> SchemaChangePlan:
        """Creates or drops the one index over all key columns of a relation to a composite
        primary key when its ``db_index`` flips - a single key column's index follows its shadow
        key field instead.

        Args:
            model: The model rendered from the target state.
            old_field: The relation's previous definition.
            new_field: The relation's new definition.
            index_dropped: Whether the index was dropped for a change of the key columns - created
                again while the relation keeps one.
        """
        index = Index(fields=(new_field.model_field_name,))
        if index_dropped:
            return self.editor.get_add_index_plan(model, index) if new_field.index else SchemaChangePlan(())
        if len(new_field.source_fields) < 2 or old_field.index == new_field.index:
            return SchemaChangePlan(())
        if new_field.index:
            return self.editor.get_add_index_plan(model, index)
        return SchemaChangePlan((SchemaStatement(self.editor.index_statements.get_index_drop_sql(model, index)),))

    def get_referencing_key_columns(
        self, target_model: type[Model], target_field_name: str, candidate_models: Sequence[type[Model]]
    ) -> list[ReferencingKeyColumns]:
        """Every relation key column elsewhere that stores values of `target_model.target_field_name`.

        Args:
            target_model: The model owning the referenced column (rendered from the new state).
            target_field_name: The referenced field's name.
            candidate_models: Every model that may hold a relation to `target_model`.

        Returns:
            One entry per referencing relation: the FK/O2O key columns of a referencing table, or
            one side's key columns of an automatic M2M through table.
        """
        referencing_key_columns: list[ReferencingKeyColumns] = []
        dialect = self.editor.client.dialect
        column_definitions = self.editor.column_definitions
        target_pk_names = target_model._meta.primary_key_attribute_names
        for candidate_model in candidate_models:
            for relation_field in candidate_model._meta.fields_map.values():
                if isinstance(relation_field, ForeignKeyFieldInstance):
                    if not self.is_same_model(relation_field.related_model, target_model):
                        continue
                    # The altered type keeps the column's nullability - a database whose type says
                    # whether it holds NULL would drop it otherwise.
                    key_columns = tuple(
                        (
                            candidate_model._meta.fields_db_projection[key_field_name],
                            column_definitions.get_altered_column_type(
                                candidate_model._meta.fields_map[key_field_name].get_column_type(dialect),
                                candidate_model._meta.fields_map[key_field_name].null,
                            ),
                        )
                        for key_field_name, to_field in zip(
                            relation_field.source_fields, relation_field.to_field_instances, strict=True
                        )
                        if to_field.model_field_name == target_field_name
                    )
                    if key_columns:
                        referencing_key_columns.append(
                            ReferencingKeyColumns(
                                candidate_model,
                                relation_field,
                                self.editor.qualify_table_name(
                                    candidate_model._meta.db_table, candidate_model._meta.schema
                                ),
                                key_columns,
                            )
                        )
                elif (
                    isinstance(relation_field, ManyToManyFieldInstance)
                    and not relation_field._generated
                    and relation_field.through_model is None
                    and target_field_name in target_pk_names
                ):
                    target_pk_field = target_model._meta.fields_map[target_field_name]
                    component_index = target_pk_names.index(target_field_name)
                    side_keys: list[str] = []
                    if self.is_same_model(candidate_model, target_model):
                        side_keys.append(relation_field.backward_keys[component_index])
                    if self.is_same_model(relation_field.related_model, target_model):
                        side_keys.append(relation_field.forward_keys[component_index])
                    if side_keys:
                        column_type = column_definitions.get_altered_column_type(
                            target_pk_field.get_column_type(dialect), False
                        )
                        referencing_key_columns.append(
                            ReferencingKeyColumns(
                                candidate_model,
                                relation_field,
                                self.editor.qualify_table_name(relation_field.through, candidate_model._meta.schema),
                                tuple((key, column_type) for key in side_keys),
                            )
                        )
        return referencing_key_columns

    def get_relation_foreign_key_restore_plan(
        self, model: type[Model], relation_field: Field[Any]
    ) -> SchemaChangePlan:
        """Prepare constraints after altering their key columns."""
        return self.get_relation_foreign_key_rebuild_plan(model, relation_field)

    def get_foreign_key_change_plan(
        self,
        model: type[Model],
        old_field: ForeignKeyFieldInstance[Model],
        new_field: ForeignKeyFieldInstance[Model],
    ) -> SchemaChangePlan:
        """Prepare an in-place constraint change where the dialect supports it."""
        return SchemaChangePlan(())

    def get_relation_foreign_key_rebuild_plan(self, model: type[Model], field: Field[Any]) -> SchemaChangePlan:
        """Prepare relation constraints, excluding declared through models and generated sides."""
        if not self.editor.client.features.supports_foreign_keys:
            return SchemaChangePlan(())
        if isinstance(field, ManyToManyFieldInstance):
            if field._generated or field.through_model is not None:
                return SchemaChangePlan(())
            return self.get_many_to_many_rebuild_plan(model, field)
        if isinstance(field, ForeignKeyFieldInstance):
            return self.get_foreign_key_rebuild_plan(model, field)
        return SchemaChangePlan(())

    def get_foreign_key_rebuild_plan(
        self, model: type[Model], field: ForeignKeyFieldInstance[Model]
    ) -> SchemaChangePlan:
        """Prepare replacement of an existing relation's constraint."""
        return self.get_foreign_key_change_plan(model, field, field)

    def get_many_to_many_rebuild_plan(
        self, model: type[Model], field: ManyToManyFieldInstance[Model]
    ) -> SchemaChangePlan:
        """Prepare both constraints of an automatic through table when supported."""
        raise UnSupportedError(
            f"Rebuilding M2M through-table constraints is not supported on {self.editor.client.dialect.name}"
        )

    def get_relation_target_check_plan(
        self,
        model: type[Model],
        old_field: ForeignKeyFieldInstance[Model],
        new_field: ForeignKeyFieldInstance[Model],
    ) -> SchemaChangePlan:
        """Compile the stored-key check and diagnostic names without reading rows."""
        columns = [model._meta.fields_db_projection[name] for name in old_field.source_fields]
        table = self.editor.qualify_table_name(model._meta.db_table, model._meta.schema)
        predicate = " OR ".join(f"{self.editor.quote(column)} IS NOT NULL" for column in columns)
        _, old_table, old_columns = self.get_relation_target_columns(old_field)
        _, new_table, new_columns = self.get_relation_target_columns(new_field)
        return SchemaChangePlan(
            (
                RelationTargetPrecondition(
                    f"SELECT count(*) AS stored_count FROM {table} WHERE {predicate}",  # nosec B608
                    f"{model.__name__}.{old_field.model_field_name}",
                    f"{old_table}({', '.join(old_columns)})",
                    f"{new_table}({', '.join(new_columns)})",
                ),
            )
        )

    @staticmethod
    def get_relation_target_columns(
        foreign_key_field: ForeignKeyFieldInstance[Model],
    ) -> tuple[str | None, str, tuple[str, ...]]:
        """The schema, table and columns a FK/O2O field's values refer to.

        Args:
            foreign_key_field: A FK/O2O field of a rendered state model.

        Returns:
            The target's schema, table and referenced column names.
        """
        related_meta = foreign_key_field.related_model._meta
        return (
            related_meta.schema,
            related_meta.db_table,
            tuple(field.source_field or field.model_field_name for field in foreign_key_field.to_field_instances),
        )

    @staticmethod
    def is_same_model(first_model: type[Model] | None, second_model: type[Model]) -> bool:
        """Whether two model classes (possibly rendered from different states) are the same model.

        Args:
            first_model: A model class, or None for an unresolved relation target.
            second_model: The model class to compare against.

        Returns:
            True when both carry the same app label and class name.
        """
        if first_model is None:
            return False
        return (first_model._meta.app, first_model.__name__) == (second_model._meta.app, second_model.__name__)

    def foreign_key_changed(self, old_field: Field[Any], new_field: Field[Any]) -> bool:
        """Whether a relation's FOREIGN KEY constraint differs between two versions of the field -
        its ``on_delete`` action, or whether there is one at all.

        Args:
            old_field: The relation field before the change.
            new_field: The relation field after it.

        Returns:
            False on a database without foreign keys, which has no constraint to change.
        """
        if not self.editor.client.features.supports_foreign_keys:
            return False
        return getattr(old_field, "on_delete", None) != getattr(new_field, "on_delete", None) or (
            self.creates_foreign_key(old_field) != self.creates_foreign_key(new_field)
        )

    def creates_foreign_key(self, relation_field: Field[Any]) -> bool:
        """Whether a relation's table gets a FOREIGN KEY constraint.

        Args:
            relation_field: A FK/O2O or many-to-many field.

        Returns:
            True for a ``db_constraint=True`` relation on a database that enforces foreign keys
            (``Features.supports_foreign_keys``).
        """
        return (
            bool(getattr(relation_field, "db_constraint", False)) and self.editor.client.features.supports_foreign_keys
        )

    def get_foreign_key_not_valid_plan(self, model: type[Model], constraint: ForeignKeyConstraint) -> SchemaChangePlan:
        """Prepare a foreign key for later validation of existing rows.

        New and updated rows must satisfy the constraint immediately. The base validates
        existing rows when adding it; dialects supporting deferred validation override this plan.

        Args:
            model: The model whose table gets the constraint.
            constraint: The foreign key constraint.
        """
        return self.editor.get_add_constraint_plan(model, constraint)
