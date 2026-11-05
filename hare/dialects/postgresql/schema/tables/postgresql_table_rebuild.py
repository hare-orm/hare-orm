from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.plans.schema_statement import SchemaStatement
from hare.dialects.base.schema.plans.schema_step import SchemaStep
from hare.dialects.base.schema.plans.schema_transaction import SchemaTransaction
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

    def get_rebuild_plan(
        self,
        old_model: type[Model],
        new_model: type[Model],
        altered_fields: Sequence[tuple[Field[Any], Field[Any]]] = (),
    ) -> SchemaChangePlan:
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
        creation = self.editor.table_creation
        foreign_keys = self.editor.foreign_key_rebuild
        model_sql_data = creation.get_model_sql_data(new_model)
        columns = self.get_copy_columns(old_model, new_model, altered_fields)
        steps: list[SchemaStep] = [
            SchemaStatement(
                f"LOCK TABLE {qualified_table} IN ACCESS EXCLUSIVE MODE", frozenset({SchemaEffect.LOCK_TABLES})
            ),
            *(SchemaStatement(sql) for sql in foreign_keys.get_incoming_foreign_key_drop_sqls(old_model)),
            # Both schema-qualified table names are escaped by the dialect identifier renderer.
            SchemaStatement(
                f"CREATE TABLE {qualified_copy} AS SELECT * FROM {qualified_table}",  # nosec B608
                frozenset({SchemaEffect.REWRITE_ROWS}),
            ),
            *(SchemaStatement(sql) for sql in self.get_table_dependent_drop_sqls(old_model)),
            SchemaStatement(f"DROP TABLE {qualified_table}", frozenset({SchemaEffect.REPLACE_TABLE})),
            *(SchemaStatement(sql) for sql in creation.get_schema_objects_before_table_sqls(new_model, safe=True)),
            SchemaStatement(model_sql_data.table_sql),
            *(
                SchemaStatement(sql)
                for trigger in meta.triggers
                for sql in self.editor.trigger_statements.get_trigger_create_sqls(new_model, trigger, safe=True)
            ),
            *(SchemaStatement(sql) for sql in model_sql_data.constraint_sqls),
        ]
        if columns:
            steps.extend(self.get_copy_plan(new_model, meta.db_table, qualified_copy, columns).steps)
        steps.append(SchemaStatement(f"DROP TABLE {qualified_copy}"))
        steps.extend(SchemaStatement(sql) for sql in self.get_sequence_resync_sqls(new_model))
        steps.extend(SchemaStatement(sql) for sql in foreign_keys.get_incoming_foreign_key_create_sqls(new_model))
        steps.extend(
            SchemaStatement(sql) for sql in creation.get_schema_objects_after_table_sqls(new_model, safe=True)
        )
        return SchemaChangePlan(
            (SchemaTransaction(SchemaChangePlan(tuple(steps))),),
            before=creation.get_definition(old_model),
            after=model_sql_data.definition,
        )

    def get_table_dependent_drop_sqls(self, model: type[Model]) -> list[str]:
        """Drops what a model declares that the drop of its table would refuse to leave or take along:
        its materialized views and views - in the reverse of their creation order - and the ownership
        of its sequences by its columns.

        Args:
            model: The model the table matches now.
        """
        meta = model._meta
        statements = self.editor.get_model_view_drop_sqls(model, if_exists=False)
        for sequence in meta.sequences:
            if sequence.owned_by is not None:
                statements.append(
                    POSTGRESQL_SEQUENCE_OWNER_NONE_TEMPLATE.format(
                        sequence=self.editor.qualify_object_name(model, sequence.name)
                    )
                )
        return statements

    def get_sequence_resync_sqls(self, model: type[Model]) -> list[str]:
        """Moves the sequence of each generated key column of a table past the keys its rows hold.

        Args:
            model: The model of the table.
        """
        meta = model._meta
        qualified_table = self.editor.qualify_table_name(meta.db_table, meta.schema)
        literals = self.editor.client.dialect.literals
        table_literal = literals.get_string_literal_sql(qualified_table)
        statements = []
        for field in meta.fields_map.values():
            if field.pk and field.generated and field.model_field_name in meta.fields_db_projection:
                column = meta.fields_db_projection[field.model_field_name]
                statements.append(
                    POSTGRESQL_RESYNC_SEQUENCE_TEMPLATE.format(
                        table_literal=table_literal,
                        column_literal=literals.get_string_literal_sql(column),
                        column=self.editor.quote(column),
                        table=qualified_table,
                    )
                )
        return statements
