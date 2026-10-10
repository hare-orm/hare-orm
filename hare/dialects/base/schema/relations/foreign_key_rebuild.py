from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from hare.ddl.constraints.foreign_key_constraint import ForeignKeyConstraint
from hare.ddl.indexes.index import Index
from hare.dialects.base.schema.data.referencing_key_columns import ReferencingKeyColumns
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.exceptions import UnSupportedError
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.migrations.exceptions import ForeignKeyTargetChangeError
from hare.models import Model


class ForeignKeyRebuild(SchemaEditorPart):
    """The foreign keys of the tables referencing a field that changes: dropped before the change,
    restored or rebuilt after it, with their on_delete and the referencing key columns found."""

    __slots__ = ()

    async def alter_referenced_field(
        self,
        model: type[Model],
        old_field: Field[Any],
        new_field: Field[Any],
        referencing_key_columns: list[ReferencingKeyColumns],
    ) -> None:
        """Changes a referenced column's type together with every key column referencing it.

        The referencing foreign key constraints are dropped first, since both sides pass through
        incompatible types on the way, and rebuilt once every column carries the new type.

        Args:
            model: The model owning the referenced column.
            old_field: The referenced field's previous definition.
            new_field: The referenced field's new definition.
            referencing_key_columns: The key columns storing the referenced column's values.
        """
        for referencing in referencing_key_columns:
            await self.drop_relation_foreign_keys(referencing.model, referencing.relation_field)
        await self.editor.alter_column(model, old_field, new_field)
        for referencing in referencing_key_columns:
            for column, sql_type in referencing.key_columns:
                changes = self.editor.ALTER_FIELD_TYPE_TEMPLATE.format(
                    column=self.editor.quote(column), sql_type=sql_type
                )
                await self.editor.run_sql(
                    self.editor.ALTER_FIELD_TEMPLATE.format(table=referencing.qualified_table, changes=changes)
                )
        for referencing in referencing_key_columns:
            await self.rebuild_relation_foreign_keys(referencing.model, referencing.relation_field)

    async def drop_relation_foreign_keys(self, model: type[Model], relation_field: Field[Any]) -> None:
        """Drops the database FK constraint(s) behind a relation field, if any. No-op by default -
        SqliteSchemaEditor rebuilds whole tables instead of altering constraints in place."""

    async def alter_composite_relation_index(
        self,
        model: type[Model],
        old_field: ForeignKeyFieldInstance[Model],
        new_field: ForeignKeyFieldInstance[Model],
    ) -> None:
        """Creates or drops the one index over all key columns of a relation to a composite
        primary key when its ``db_index`` flips - a single key column's index follows its shadow
        key field instead.

        Args:
            model: The model rendered from the target state.
            old_field: The relation's previous definition.
            new_field: The relation's new definition.
        """
        if len(new_field.source_fields) < 2 or old_field.index == new_field.index:
            return
        index = Index(fields=(new_field.model_field_name,))
        if new_field.index:
            await self.editor.add_index(model, index)
        else:
            await self.editor.remove_index(model, index)

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
        target_pk_names = target_model._meta.primary_key_attribute_names
        for candidate_model in candidate_models:
            for relation_field in candidate_model._meta.fields_map.values():
                if isinstance(relation_field, ForeignKeyFieldInstance):
                    if not self.is_same_model(relation_field.related_model, target_model):
                        continue
                    key_columns = tuple(
                        (
                            candidate_model._meta.fields_db_projection[key_field_name],
                            candidate_model._meta.fields_map[key_field_name].get_column_type(
                                self.editor.client.dialect
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
                        column_type = target_pk_field.get_column_type(self.editor.client.dialect)
                        referencing_key_columns.append(
                            ReferencingKeyColumns(
                                candidate_model,
                                relation_field,
                                self.editor.qualify_table_name(relation_field.through, candidate_model._meta.schema),
                                tuple((key, column_type) for key in side_keys),
                            )
                        )
        return referencing_key_columns

    async def restore_relation_foreign_keys(self, model: type[Model], relation_field: Field[Any]) -> None:
        """Re-creates the FK constraint(s) of a relation whose key column was just altered.

        Args:
            model: The model owning `relation_field`, rendered from the new state.
            relation_field: The altered relation field.
        """
        await self.rebuild_relation_foreign_keys(model, relation_field)

    async def alter_foreign_key_on_delete(
        self,
        model: type[Model],
        db_field: str,
        old_foreign_key_field: ForeignKeyFieldInstance[Model],
        new_foreign_key_field: ForeignKeyFieldInstance[Model],
    ) -> None:
        """Applies an ``on_delete=`` change to the foreign key constraint. A no-op by default - a
        dialect altering tables in place replaces the constraint.
        """

    async def rebuild_relation_foreign_keys(self, model: type[Model], field: Field[Any]) -> None:
        """Re-emits the database FK constraint(s) behind a relation field from its current
        metadata (ON DELETE action and db_constraint), replacing whatever the database has now.

        Args:
            model: The model owning ``field``.
            field: A FK/O2O field, or an M2M field with an auto-managed through table (anything
                else is left untouched). Nothing happens on a database without foreign keys.
        """
        if not self.editor.client.features.supports_foreign_keys:
            return
        if isinstance(field, ManyToManyFieldInstance):
            if field._generated or field.through_model is not None:
                return
            await self.rebuild_many_to_many_through_foreign_keys(model, field)
            return
        if isinstance(field, ForeignKeyFieldInstance):
            await self.rebuild_foreign_key_constraints(model, field)

    async def rebuild_foreign_key_constraints(
        self, model: type[Model], foreign_key_field: ForeignKeyFieldInstance[Model]
    ) -> None:
        """Replaces a FK/O2O field's own constraint - see rebuild_relation_foreign_keys()."""
        await self.alter_foreign_key_on_delete(
            model, foreign_key_field.source_fields[0], foreign_key_field, foreign_key_field
        )

    async def rebuild_many_to_many_through_foreign_keys(
        self, model: type[Model], field: ManyToManyFieldInstance[Model]
    ) -> None:
        """Replaces both FK constraints of an auto-managed M2M through table - see
        rebuild_relation_foreign_keys().

        Raises:
            UnSupportedError: On a dialect without its own implementation.
        """
        raise UnSupportedError(
            f"Rebuilding M2M through-table constraints is not supported on {self.editor.client.dialect.name}"
        )

    async def raise_if_relation_values_need_remapping(
        self,
        model: type[Model],
        old_foreign_key_field: ForeignKeyFieldInstance[Model],
        new_foreign_key_field: ForeignKeyFieldInstance[Model],
    ) -> None:
        """Refuses to repoint a FK/O2O at another target column while rows still store key values.

        Stored values name rows through the old target column - kept as they are, they'd silently
        point at the wrong rows (or none) through the new one.

        Args:
            model: The model owning the relation, rendered from the current state.
            old_foreign_key_field: The relation's current definition.
            new_foreign_key_field: The relation's new definition.

        Raises:
            ForeignKeyTargetChangeError: At least one row stores a key value.
        """
        if self.editor.collect_sql:
            return
        columns = [
            model._meta.fields_db_projection[key_field_name] for key_field_name in old_foreign_key_field.source_fields
        ]
        qualified_table = self.editor.qualify_table_name(model._meta.db_table, model._meta.schema)
        predicate = " OR ".join(f"{self.editor.quote(column)} IS NOT NULL" for column in columns)
        rows = await self.editor.client.execute_dicts(
            f"SELECT count(*) AS stored_count FROM {qualified_table} WHERE {predicate}"  # nosec B608
        )
        stored_count = rows[0]["stored_count"]
        if not stored_count:
            return
        _old_schema, old_table, old_columns = self.get_relation_target_columns(old_foreign_key_field)
        _new_schema, new_table, new_columns = self.get_relation_target_columns(new_foreign_key_field)
        raise ForeignKeyTargetChangeError(
            f"Cannot repoint {model.__name__}.{old_foreign_key_field.model_field_name} from "
            f"{old_table}({', '.join(old_columns)}) to {new_table}({', '.join(new_columns)}) - "
            f"{stored_count} row(s) store values of the old target column, which would silently "
            "refer to the wrong rows (or none) afterwards. Migrate the data by hand instead: add a "
            "new relation field targeting the new column, fill it with RunPython/RunSQL, then "
            "remove the old field."
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

    async def add_foreign_key_constraint_not_valid(self, model: type[Model], constraint: ForeignKeyConstraint) -> None:
        """Adds a FOREIGN KEY that only new and updated rows must satisfy - the existing rows are
        checked later, by ``validate_constraint()``. A dialect that can't leave the existing rows
        unchecked (``Features.supports_not_valid_constraints`` False) adds the constraint as usual.

        Args:
            model: The model whose table gets the constraint.
            constraint: The foreign key constraint.
        """
        await self.editor.add_constraint(model, constraint)
