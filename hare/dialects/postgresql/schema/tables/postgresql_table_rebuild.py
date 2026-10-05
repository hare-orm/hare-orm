from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.dialects.base.schema.tables.table_rebuild import TableRebuild
from hare.dialects.postgresql.schema.constants import (
    POSTGRESQL_RESYNC_SEQUENCE_TEMPLATE,
    POSTGRESQL_SEQUENCE_OWNER_NONE_TEMPLATE,
    POSTGRESQL_TABLE_COPY_SUFFIX,
)
from hare.sql.identifiers import Identifiers

if TYPE_CHECKING:
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor
    from hare.fields.field import Field
    from hare.models import Model


class PostgresqlTableRebuild(TableRebuild):
    """TableRebuild as PostgreSQL makes it: the names of a table's primary key and unique constraints
    are the names of their indexes, taken in the whole schema while the old table exists - the rows
    wait in a copy while the table is dropped and created anew under the same names. The views of the
    model, which depend on the table, are dropped before it and the objects of the model created again
    after it; its sequences outlive it, their values too."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    async def remake_table(
        self,
        old_model: type[Model],
        new_model: type[Model],
        altered_fields: Sequence[tuple[Field[Any], Field[Any]]] = (),
    ) -> None:
        """Creates a table anew from the model it is rebuilt to, with its rows: they are kept in a copy
        of the table while the table is dropped and created from the model with its partitions,
        indexes, constraints, comments and triggers; then they are put back, the sequences of
        generated keys moved past them and the foreign keys of other tables referencing the table set
        again.

        Args:
            old_model: The model the table matches now.
            new_model: The model the table is rebuilt to.
            altered_fields: (old, new) fields paired otherwise than by name - a renamed field.
        """
        meta = new_model._meta
        qualified_table = self.editor.qualify_table_name(meta.db_table, meta.schema)
        copy_table_name = Identifiers.get_within_limit(f"{meta.db_table}{POSTGRESQL_TABLE_COPY_SUFFIX}")
        qualified_copy = self.editor.qualify_table_name(copy_table_name, meta.schema)
        # The foreign keys of other tables referencing the table - the relations of the state.
        await self.editor.foreign_key_rebuild.drop_incoming_foreign_keys(old_model)
        await self.editor.run_sql(f"CREATE TABLE {qualified_copy} AS SELECT * FROM {qualified_table}")  # nosec B608
        await self.drop_table_dependents(old_model)
        await self.editor.run_sql(f"DROP TABLE {qualified_table}")
        await self.editor.run_sqls(
            self.editor.table_creation.get_schema_objects_before_table_sqls(new_model, safe=True)
        )
        model_sql_data = self.editor.table_creation.get_model_sql_data(new_model)
        await self.editor.run_sql(model_sql_data.table_sql)
        for trigger in meta.triggers:
            for statement in self.editor.trigger_statements.get_trigger_create_sqls(new_model, trigger, safe=True):
                await self.editor.run_sql(statement)
        for constraint_sql in model_sql_data.constraint_sqls:
            await self.editor.run_sql(constraint_sql)
        column_mapping = self.get_column_mapping(old_model, new_model, altered_fields)
        if column_mapping:
            await self.copy_table_rows(new_model, meta.db_table, copy_table_name, column_mapping)
        await self.editor.run_sql(f"DROP TABLE {qualified_copy}")
        await self.resync_generated_key_sequences(new_model)
        await self.editor.foreign_key_rebuild.add_incoming_foreign_keys(new_model)
        # The views read the rows put back - a materialized view is filled from them.
        await self.editor.run_sqls(
            self.editor.table_creation.get_schema_objects_after_table_sqls(new_model, safe=True)
        )

    async def drop_table_dependents(self, model: type[Model]) -> None:
        """Drops what a model declares that the drop of its table would refuse to leave or take along:
        its materialized views and views - in the reverse of their creation order - and the ownership
        of its sequences by its columns.

        Args:
            model: The model the table matches now.
        """
        meta = model._meta
        for materialized_view in reversed(meta.materialized_views):
            await self.editor.materialized_views.drop_materialized_view(model, materialized_view)
        for view in reversed(meta.views):
            await self.editor.views.drop_view(model, view)
        for sequence in meta.sequences:
            if sequence.owned_by is not None:
                await self.editor.run_sql(
                    POSTGRESQL_SEQUENCE_OWNER_NONE_TEMPLATE.format(
                        sequence=self.editor.qualify_object_name(model, sequence.name)
                    )
                )

    async def resync_generated_key_sequences(self, model: type[Model]) -> None:
        """Moves the sequence of each generated key column of a table past the keys its rows hold.

        Args:
            model: The model of the table.
        """
        meta = model._meta
        qualified_table = self.editor.qualify_table_name(meta.db_table, meta.schema)
        table_literal = "'" + qualified_table.replace("'", "''") + "'"
        for field in meta.fields_map.values():
            if field.pk and field.generated and field.model_field_name in meta.fields_db_projection:
                column = meta.fields_db_projection[field.model_field_name]
                await self.editor.run_sql(
                    POSTGRESQL_RESYNC_SEQUENCE_TEMPLATE.format(
                        table_literal=table_literal,
                        column_literal="'" + column.replace("'", "''") + "'",
                        column=self.editor.quote(column),
                        table=qualified_table,
                    )
                )
