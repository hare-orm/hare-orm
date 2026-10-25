from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, cast

from hare.ddl.constraints.foreign_key_constraint import ForeignKeyConstraint
from hare.ddl.generated_names import GeneratedNames
from hare.dialects.base.schema.relations.foreign_key_rebuild import ForeignKeyRebuild
from hare.fields.enums import OnDelete
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.models import Model

if TYPE_CHECKING:
    from typing import Any

    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlForeignKeyRebuild(ForeignKeyRebuild):
    """ForeignKeyRebuild as PostgreSQL writes it - a constraint is dropped and added in place, by the name
    its key columns give it; whether one exists is the relation's own declaration, never the catalog's."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    async def drop_foreign_key(self, table_name: str, column_names: Sequence[str], schema: str | None) -> None:
        """Drops the foreign key over exactly ``column_names`` of a table.

        Args:
            table_name: The table owning the constraint.
            column_names: The constraint's own columns, in order.
            schema: The table's schema.
        """
        await self.editor.run_sql(
            self.editor.DELETE_CONSTRAINT_TEMPLATE.format(
                table=self.editor.qualify_table_name(table_name, schema),
                name=self.editor.quote(GeneratedNames.get_foreign_key_name(table_name, column_names)),
            )
        )

    async def drop_relation_foreign_keys(self, model: type[Model], relation_field: Field[Any]) -> None:
        if not self.creates_foreign_key(relation_field):
            return
        if isinstance(relation_field, ManyToManyFieldInstance):
            if relation_field._generated or relation_field.through_model is not None:
                return
            for side_keys in (relation_field.backward_keys, relation_field.forward_keys):
                await self.drop_foreign_key(relation_field.through, side_keys, model._meta.schema)
            return
        if isinstance(relation_field, ForeignKeyFieldInstance):
            columns = [
                model._meta.fields_db_projection[key_field_name] for key_field_name in relation_field.source_fields
            ]
            await self.drop_foreign_key(model._meta.db_table, columns, model._meta.schema)

    async def add_relation_foreign_key(
        self, model: type[Model], foreign_key_field: ForeignKeyFieldInstance[Model]
    ) -> None:
        """Adds a FK/O2O field's constraint - nothing for one declared without it.

        Args:
            model: The model owning the relation.
            foreign_key_field: The relation.
        """
        if not self.creates_foreign_key(foreign_key_field):
            return
        if len(foreign_key_field.source_fields) > 1:
            constraint = self.editor.table_creation.get_composite_foreign_key_constraint(model, foreign_key_field)
        else:
            constraint = self.editor.table_creation.get_single_column_foreign_key_constraint(
                model, foreign_key_field.source_fields[0]
            )
        await self.editor.run_sql(self.editor.table_creation.get_add_foreign_key_sql(model, constraint, safe=False))

    async def alter_foreign_key_on_delete(
        self,
        model: type[Model],
        db_field: str,
        old_foreign_key_field: ForeignKeyFieldInstance[Model],
        new_foreign_key_field: ForeignKeyFieldInstance[Model],
    ) -> None:
        await self.drop_relation_foreign_keys(model, old_foreign_key_field)
        await self.add_relation_foreign_key(model, new_foreign_key_field)

    async def rebuild_foreign_key_constraints(
        self, model: type[Model], foreign_key_field: ForeignKeyFieldInstance[Model]
    ) -> None:
        """Adds back a FK/O2O field's constraint dropped for a change of its key columns."""
        await self.add_relation_foreign_key(model, foreign_key_field)

    async def rebuild_many_to_many_through_foreign_keys(
        self,
        model: type[Model],
        field: ManyToManyFieldInstance[Model],
    ) -> None:
        """Replaces the FK constraints of an auto-managed through table with the field's current ON
        DELETE action and aligns the key columns' nullability with it
        (only ON DELETE SET NULL needs nullable columns)."""
        from hare.query.key_columns import KeyColumns

        schema = model._meta.schema
        through_table = field.through
        qualified_through = self.editor.qualify_table_name(through_table, schema)
        nullability_template = (
            self.editor.ALTER_FIELD_NULL_TEMPLATE
            if field.db_on_delete == OnDelete.SET_NULL
            else self.editor.ALTER_FIELD_NOT_NULL_TEMPLATE
        )
        sides = ((field.backward_keys, model._meta), (field.forward_keys, field.related_model._meta))
        for side_keys, target_meta in sides:
            for key in side_keys:
                await self.editor.run_sql(
                    self.editor.ALTER_FIELD_TEMPLATE.format(
                        table=qualified_through, changes=nullability_template.format(column=self.editor.quote(key))
                    )
                )
            if not self.creates_foreign_key(field):
                continue
            pk_columns = KeyColumns.get_source_columns(target_meta)
            constraint_sql = self.editor.FOREIGN_KEY_CONSTRAINT_CREATE_TEMPLATE.format(
                name=self.editor.quote(GeneratedNames.get_foreign_key_name(through_table, side_keys)),
                fields=", ".join(self.editor.quote(key) for key in side_keys),
                table=self.editor.qualify_table_name(target_meta.db_table, target_meta.schema),
                to_fields=", ".join(self.editor.quote(column) for column in pk_columns),
                on_delete=field.db_on_delete,
            )
            await self.editor.run_sql(
                self.editor.ADD_CONSTRAINT_TEMPLATE.format(table=qualified_through, constraint=constraint_sql)
            )

    def get_incoming_relations(
        self, model: type[Model]
    ) -> list[tuple[type[Model], Field[Any], tuple[str, ...] | None]]:
        """The relations of other tables with a FOREIGN KEY to a model's table - read from the models the
        state links to it, never from the catalog.

        Args:
            model: The model rendered from the state.

        Returns:
            (the model declaring the relation, the relation, the keys of the through table side
            referencing the table - None for a FK/O2O) per relation.
        """
        meta = model._meta
        candidate_models: dict[tuple[str | None, str], type[Model]] = {}
        relation_fields = [*meta.fields_map.values(), *meta.hidden_backward_relations.values()]
        for relation_field in relation_fields:
            related_model = getattr(relation_field, "related_model", None)
            if related_model is not None:
                candidate_models[(related_model._meta.app, related_model.__name__)] = related_model
        candidate_models[(meta.app, model.__name__)] = model
        incoming: list[tuple[type[Model], Field[Any], tuple[str, ...] | None]] = []
        for candidate_model in candidate_models.values():
            for relation_field in candidate_model._meta.fields_map.values():
                if not self.creates_foreign_key(relation_field):
                    continue
                if isinstance(relation_field, ForeignKeyFieldInstance):
                    # A relation of the table to itself goes with the table.
                    if self.is_same_model(relation_field.related_model, model) and not self.is_same_model(
                        candidate_model, model
                    ):
                        incoming.append((candidate_model, relation_field, None))
                elif (
                    isinstance(relation_field, ManyToManyFieldInstance)
                    and not relation_field._generated
                    and relation_field.through_model is None
                ):
                    if self.is_same_model(candidate_model, model):
                        incoming.append((candidate_model, relation_field, tuple(relation_field.backward_keys)))
                    if self.is_same_model(relation_field.related_model, model):
                        incoming.append((candidate_model, relation_field, tuple(relation_field.forward_keys)))
        return incoming

    async def drop_incoming_foreign_keys(self, model: type[Model]) -> None:
        """Drops the foreign keys of other tables referencing a model's table.

        Args:
            model: The model rendered from the state.
        """
        for referencing_model, relation_field, side_keys in self.get_incoming_relations(model):
            if side_keys is None:
                await self.drop_relation_foreign_keys(referencing_model, relation_field)
            else:
                through_field = cast("ManyToManyFieldInstance[Model]", relation_field)
                await self.drop_foreign_key(through_field.through, side_keys, referencing_model._meta.schema)

    async def add_incoming_foreign_keys(self, model: type[Model]) -> None:
        """Adds back the foreign keys of other tables referencing a model's table.

        Args:
            model: The model rendered from the state.
        """
        from hare.query.key_columns import KeyColumns

        for referencing_model, relation_field, side_keys in self.get_incoming_relations(model):
            if side_keys is None:
                await self.add_relation_foreign_key(
                    referencing_model, cast("ForeignKeyFieldInstance[Model]", relation_field)
                )
                continue
            through_field = cast("ManyToManyFieldInstance[Model]", relation_field)
            constraint_sql = self.editor.FOREIGN_KEY_CONSTRAINT_CREATE_TEMPLATE.format(
                name=self.editor.quote(GeneratedNames.get_foreign_key_name(through_field.through, side_keys)),
                fields=", ".join(self.editor.quote(key) for key in side_keys),
                table=self.editor.qualify_table_name(model._meta.db_table, model._meta.schema),
                to_fields=", ".join(
                    self.editor.quote(column) for column in KeyColumns.get_source_columns(model._meta)
                ),
                on_delete=through_field.db_on_delete,
            )
            await self.editor.run_sql(
                self.editor.ADD_CONSTRAINT_TEMPLATE.format(
                    table=self.editor.qualify_table_name(through_field.through, referencing_model._meta.schema),
                    constraint=constraint_sql,
                )
            )

    async def add_foreign_key_constraint_not_valid(self, model: type[Model], constraint: ForeignKeyConstraint) -> None:
        await self.editor.run_sql(
            self.editor.ADD_CONSTRAINT_TEMPLATE.format(
                table=self.editor.qualify_table_name(model._meta.db_table, model._meta.schema),
                constraint=f"{self.editor.table_creation.get_foreign_key_constraint_clause(constraint)} NOT VALID",
            )
        )
