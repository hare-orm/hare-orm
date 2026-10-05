from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.ddl.constraints.foreign_key_constraint import ForeignKeyConstraint
from hare.ddl.generated_names import GeneratedNames
from hare.dialects.base.schema.relations.foreign_key_rebuild import ForeignKeyRebuild
from hare.fields.enums import OnDelete
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.models import Model

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlForeignKeyRebuild(ForeignKeyRebuild):
    """ForeignKeyRebuild as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    async def drop_relation_foreign_keys(self, model: type[Model], relation_field: Field[Any]) -> None:
        if isinstance(relation_field, ManyToManyFieldInstance):
            if relation_field._generated or relation_field.through_model is not None:
                return
            for side_keys in (relation_field.backward_keys, relation_field.forward_keys):
                await self.drop_foreign_key_by_columns(relation_field.through, side_keys, model._meta.schema)
            return
        if isinstance(relation_field, ForeignKeyFieldInstance):
            columns = [
                model._meta.fields_db_projection[key_field_name] for key_field_name in relation_field.source_fields
            ]
            await self.drop_foreign_key_by_columns(model._meta.db_table, columns, model._meta.schema)

    async def alter_foreign_key_on_delete(
        self,
        model: type[Model],
        db_field: str,
        old_foreign_key_field: ForeignKeyFieldInstance[Model],
        new_foreign_key_field: ForeignKeyFieldInstance[Model],
    ) -> None:
        table_name = model._meta.db_table
        schema = model._meta.schema
        if len(new_foreign_key_field.source_fields) > 1:
            # Composite target: db_field is only the FIRST shadow column (see alter_field()'s
            # own call site comment) - the real constraint spans all of them, so it has to be
            # looked up and rebuilt by the full column set, not db_field alone.
            constraint_name = await self.get_composite_foreign_key_constraint_name_from_db(
                table_name, new_foreign_key_field.source_fields, schema
            )
            if constraint_name is not None:
                await self.editor.run_sql(
                    self.editor.DELETE_CONSTRAINT_TEMPLATE.format(
                        table=self.editor.qualify_table_name(table_name, schema),
                        name=self.editor.quote(constraint_name),
                    )
                )
            if not self.creates_foreign_key(new_foreign_key_field):
                # db_constraint=False: the constraint dropped above isn't created again.
                return
            new_composite_constraint = self.editor.table_creation.get_composite_foreign_key_constraint(
                model, new_foreign_key_field
            )
            if constraint_name is not None:
                # Keeps the constraint's existing name, which may not follow today's naming.
                new_composite_constraint = ForeignKeyConstraint(
                    fields=new_composite_constraint.fields,
                    to_table=new_composite_constraint.to_table,
                    to_fields=new_composite_constraint.to_fields,
                    on_delete=new_composite_constraint.on_delete,
                    name=constraint_name,
                )
            await self.editor.add_constraint(model, new_composite_constraint)
            return
        constraint_name = await self.get_foreign_key_constraint_name_from_db(table_name, db_field, schema)
        qualified_table = self.editor.qualify_table_name(table_name, schema)
        if constraint_name is not None:
            await self.editor.run_sql(
                self.editor.DELETE_CONSTRAINT_TEMPLATE.format(
                    table=qualified_table, name=self.editor.quote(constraint_name)
                )
            )
        if not self.creates_foreign_key(new_foreign_key_field):
            # Same reasoning as the composite branch above: db_constraint=False on the new field
            # means no FK constraint at all - nothing more to (re)build once the old one (if any)
            # is dropped.
            return
        related_model = new_foreign_key_field.related_model
        to_field_name = (
            new_foreign_key_field.to_field_instance.source_field
            or new_foreign_key_field.to_field_instance.model_field_name
        )
        if constraint_name is None:
            # No constraint existed to reuse the name of (old field had db_constraint=False) -
            # generate a fresh one, the same naming convention a brand-new column's own inline FK
            # reference uses (_get_fk_field_definition's identical call).
            constraint_name = GeneratedNames.get_foreign_key_name(
                table_name, (db_field,), related_model._meta.db_table, (to_field_name,)
            )
        new_constraint = (
            f"CONSTRAINT {self.editor.quote(constraint_name)} FOREIGN KEY ({self.editor.quote(db_field)}) "
            f"REFERENCES {self.editor.qualify_table_name(related_model._meta.db_table, related_model._meta.schema)} "
            f"({self.editor.quote(to_field_name)}) ON DELETE {new_foreign_key_field.db_on_delete}"
        )
        await self.editor.run_sql(
            self.editor.ADD_CONSTRAINT_TEMPLATE.format(table=qualified_table, constraint=new_constraint)
        )

    async def rebuild_many_to_many_through_foreign_keys(
        self, model: type[Model], field: ManyToManyFieldInstance[Model]
    ) -> None:
        """Drops and re-adds both sides' FK constraints of an auto-managed through table with the
        field's current ON DELETE action, keeping each existing constraint's name, and aligns the
        key columns' nullability with it (only ON DELETE SET NULL needs nullable columns)."""
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
            if len(side_keys) == 1:
                constraint_name = await self.get_foreign_key_constraint_name_from_db(
                    through_table, side_keys[0], schema
                )
            else:
                constraint_name = await self.get_composite_foreign_key_constraint_name_from_db(
                    through_table, side_keys, schema
                )
            if constraint_name is not None:
                await self.editor.run_sql(
                    self.editor.DELETE_CONSTRAINT_TEMPLATE.format(
                        table=qualified_through, name=self.editor.quote(constraint_name)
                    )
                )
            for key in side_keys:
                await self.editor.run_sql(
                    self.editor.ALTER_FIELD_TEMPLATE.format(
                        table=qualified_through, changes=nullability_template.format(column=self.editor.quote(key))
                    )
                )
            if not self.creates_foreign_key(field):
                continue
            pk_columns = KeyColumns.get_source_columns(target_meta)
            if constraint_name is None:
                constraint_name = (
                    GeneratedNames.get_foreign_key_name(
                        through_table, (side_keys[0],), target_meta.db_table, (pk_columns[0],)
                    )
                    if len(side_keys) == 1
                    else GeneratedNames.get_foreign_key_name(
                        through_table, side_keys, target_meta.db_table, pk_columns
                    )
                )
            constraint_sql = self.editor.FOREIGN_KEY_CONSTRAINT_CREATE_TEMPLATE.format(
                name=self.editor.quote(constraint_name),
                fields=", ".join(self.editor.quote(key) for key in side_keys),
                table=self.editor.qualify_table_name(target_meta.db_table, target_meta.schema),
                to_fields=", ".join(self.editor.quote(column) for column in pk_columns),
                on_delete=field.db_on_delete,
            )
            await self.editor.run_sql(
                self.editor.ADD_CONSTRAINT_TEMPLATE.format(table=qualified_through, constraint=constraint_sql)
            )

    async def add_foreign_key_constraint_not_valid(self, model: type[Model], constraint: ForeignKeyConstraint) -> None:
        await self.editor.run_sql(
            self.editor.ADD_CONSTRAINT_TEMPLATE.format(
                table=self.editor.qualify_table_name(model._meta.db_table, model._meta.schema),
                constraint=f"{self.editor.table_creation.get_foreign_key_constraint_clause(constraint)} NOT VALID",
            )
        )

    async def drop_foreign_key_by_columns(
        self, table_name: str, column_names: Sequence[str], schema: str | None
    ) -> None:
        """Drops the FK constraint defined on exactly `column_names` of a table, if there is one.

        Args:
            table_name: The table owning the constraint.
            column_names: The constraint's own columns, in order.
            schema: The table's schema.
        """
        if len(column_names) == 1:
            constraint_name = await self.get_foreign_key_constraint_name_from_db(table_name, column_names[0], schema)
        else:
            constraint_name = await self.get_composite_foreign_key_constraint_name_from_db(
                table_name, column_names, schema
            )
        if constraint_name is None:
            return
        await self.editor.run_sql(
            self.editor.DELETE_CONSTRAINT_TEMPLATE.format(
                table=self.editor.qualify_table_name(table_name, schema), name=self.editor.quote(constraint_name)
            )
        )

    async def get_composite_foreign_key_constraint_name_from_db(
        self, table_name: str, column_names: Sequence[str], schema: str | None = None
    ) -> str | None:
        """Same idea as ConstraintNames.get_unique_constraint_names_from_db() above, for a composite (multi-
        column) FK constraint instead of a UniqueConstraint - matches con.contype = 'f' and the
        exact, ordered column set."""
        query = (
            "SELECT con.conname "
            "FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace "
            "WHERE rel.relname = $1 "  # nosec B608
            "AND con.contype = 'f' "
            "AND nsp.nspname = COALESCE($2::text, current_schema()) "
            "AND ARRAY("
            "  SELECT att.attname::text"
            "  FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)"
            "  JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = k.attnum"
            "  ORDER BY k.ord"
            ") = $3::text[]"
        )
        _, rows = await self.editor.client.execute(query, [table_name, schema, list(column_names)])
        return rows[0]["conname"] if rows else None

    async def get_foreign_key_constraint_name_from_db(
        self, table_name: str, column_name: str, schema: str | None = None
    ) -> str | None:
        """The name ``pg_constraint`` has for one column's foreign key - a column-level ``REFERENCES``
        clause gets a name PostgreSQL chose.
        """
        query = (
            "SELECT con.conname "
            "FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace "
            "JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = ANY(con.conkey) "
            "WHERE rel.relname = $1 "  # nosec B608
            "AND nsp.nspname = COALESCE($2::text, current_schema()) "
            "AND con.contype = 'f' "
            "AND att.attname = $3 "
            "AND cardinality(con.conkey) = 1"
        )
        _, rows = await self.editor.client.execute(query, [table_name, schema, column_name])
        return rows[0]["conname"] if rows else None
