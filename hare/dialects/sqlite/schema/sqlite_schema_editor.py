from __future__ import annotations

from collections.abc import AsyncGenerator, Iterable, Sequence
from contextlib import asynccontextmanager
from copy import copy
from typing import TYPE_CHECKING, Any, cast

from hare.ddl.conditions.constraint_condition import ConstraintCondition
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.generated_names import GeneratedNames
from hare.ddl.indexes.index import Index
from hare.ddl.indexes.partial_index import PartialIndex
from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
from hare.dialects.sqlite.client.sqlite_client import SqliteClient
from hare.dialects.sqlite.constants import (
    SPATIALITE_INDEX_RESULT_COLUMN,
    SQLITE_AUTOINDEX_PREFIX,
    SQLITE_DEFAULT_FOREIGN_KEYS,
)
from hare.dialects.sqlite.indexes.own_table_index import OwnTableIndex
from hare.dialects.sqlite.indexes.spatialite_index import SpatialiteIndex
from hare.dialects.sqlite.schema.columns.sqlite_column_backfill import SqliteColumnBackfill
from hare.dialects.sqlite.schema.columns.sqlite_column_narrowing_check import SqliteColumnNarrowingCheck
from hare.dialects.sqlite.schema.columns.sqlite_column_type_changes import SqliteColumnTypeChanges
from hare.dialects.sqlite.schema.constants import (
    SQLITE_CHECK_KEYWORD,
    SQLITE_DROP_COLUMN_BLOCKERS_SQL,
    SQLITE_EXPRESSION_INDEX_COLUMN,
    SQLITE_FOREIGN_KEY_CHECK_SQL,
    SQLITE_PRAGMA_ENABLED_VALUES,
)
from hare.dialects.sqlite.schema.constraints.sqlite_constraint_names import SqliteConstraintNames
from hare.dialects.sqlite.schema.constraints.sqlite_constraint_statements import SqliteConstraintStatements
from hare.dialects.sqlite.schema.indexes.sqlite_generated_index_names import SqliteGeneratedIndexNames
from hare.dialects.sqlite.schema.indexes.sqlite_index_statements import SqliteIndexStatements
from hare.dialects.sqlite.schema.relations.sqlite_foreign_key_rebuild import SqliteForeignKeyRebuild
from hare.dialects.sqlite.schema.runtime_statements.sqlite_table_clearing import SqliteTableClearing
from hare.dialects.sqlite.schema.runtime_statements.sqlite_tenant_conditions import SqliteTenantConditions
from hare.dialects.sqlite.schema.tables.sqlite_table_comments import SqliteTableComments
from hare.dialects.sqlite.schema.tables.sqlite_table_rebuild import SqliteTableRebuild
from hare.dialects.sqlite.schema.triggers.sqlite_trigger_statements import SqliteTriggerStatements
from hare.exceptions import IntegrityError, OperationalError
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.models import Model
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field


class SqliteSchemaEditor(BaseSchemaEditor):
    column_type_changes_class = SqliteColumnTypeChanges
    column_type_changes: SqliteColumnTypeChanges
    column_backfill_class = SqliteColumnBackfill
    column_narrowing_check_class = SqliteColumnNarrowingCheck
    table_rebuild_class = SqliteTableRebuild
    table_comments_class = SqliteTableComments
    foreign_key_rebuild_class = SqliteForeignKeyRebuild
    index_statements_class = SqliteIndexStatements
    generated_index_names_class = SqliteGeneratedIndexNames
    constraint_statements_class = SqliteConstraintStatements
    constraint_names_class = SqliteConstraintNames
    trigger_statements_class = SqliteTriggerStatements
    table_clearing_class = SqliteTableClearing
    tenant_conditions_class = SqliteTenantConditions

    DELETE_TABLE_TEMPLATE = "DROP TABLE {table}"
    DELETE_FIELD_TEMPLATE = "ALTER TABLE {table} DROP COLUMN {column}"
    DROP_INDEX_IF_EXISTS_TEMPLATE = "DROP INDEX IF EXISTS {name}"
    #: SQLite has no ALTER TABLE ... RENAME CONSTRAINT at all - see rename_constraint() below.
    RENAME_CONSTRAINT_TEMPLATE = None
    #: A SQLite trigger has no function and no FOR EACH clause - the body runs once per row.
    #: {trigger_name} is passed already quoted.
    TRIGGER_CREATE_TEMPLATE = (
        "CREATE TRIGGER {trigger_name} {timing} {on} ON {table}\n{when_clause}BEGIN\n{body}\nEND;"
    )
    TRIGGER_CREATE_IF_NOT_EXISTS_TEMPLATE = TRIGGER_CREATE_TEMPLATE.replace(
        "CREATE TRIGGER", "CREATE TRIGGER IF NOT EXISTS", 1
    )
    TRIGGER_DROP_TEMPLATE = "DROP TRIGGER {trigger_name}"
    #: True while constraint_checking_disabled() has switched off foreign key enforcement that
    #: the connection normally has on - check_constraints() only verifies in that window.
    foreign_key_check_required = False

    async def run_sql(self, sql: str) -> None:
        """Runs DDL. In atomic mode each statement goes through ``execute()`` - ``executescript()``
        issues an implicit COMMIT. In ``collect_sql`` mode the SQL is only collected.
        """
        if self.collect_sql:
            await super().run_sql(sql)
            return
        if self.atomic_migration or self.client.is_transaction_client:
            for statement in SqliteClient.split_script(sql):
                await self.client.execute(statement)
        else:
            await self.client.execute_script(sql)

    @asynccontextmanager
    async def constraint_checking_disabled(self) -> AsyncGenerator[None]:
        """Switches ``PRAGMA foreign_keys`` off for the block and restores what was configured. A table
        rebuild drops the table, and with foreign keys on, that deletes its rows first and cascades
        into the tables referencing it. The pragma can only be changed outside a transaction - the
        executor enters this block before opening one.
        """
        original = cast("SqliteClient", self.client).pragmas.get("foreign_keys", SQLITE_DEFAULT_FOREIGN_KEYS)
        await self.client.execute_script("PRAGMA foreign_keys = OFF")
        self.foreign_key_check_required = str(original).strip().lower() in SQLITE_PRAGMA_ENABLED_VALUES
        try:
            yield
        finally:
            self.foreign_key_check_required = False
            await self.client.execute_script(f"PRAGMA foreign_keys = {original}")

    async def check_constraints(self) -> None:
        """Raises if the schema changes made while foreign key enforcement was off left
        dangling references behind.

        Raises:
            IntegrityError: ``PRAGMA foreign_key_check`` reported at least one violation.
        """
        if self.collect_sql or not self.foreign_key_check_required:
            return
        _, rows = await self.client.execute(SQLITE_FOREIGN_KEY_CHECK_SQL)
        if not rows:
            return
        violations = "; ".join(
            f"{row['table']} rowid={row['rowid']} references missing row in {row['parent']}" for row in rows
        )
        raise IntegrityError(f"Migration left {len(rows)} foreign key violation(s): {violations}")

    async def add_field(self, model: type[Model], field_name: str, *, foreign_key_not_valid: bool = False) -> None:
        # SQLite never checks the existing rows against a FOREIGN KEY it adds - foreign_key_not_valid
        # changes nothing.
        field = model._meta.fields_map[field_name]
        if isinstance(field, ManyToManyFieldInstance):
            table_string = self.table_creation.get_many_to_many_table_definition(model, field)
            if table_string:
                await self.run_sql(table_string)
            return
        if isinstance(field, ForeignKeyFieldInstance) and len(field.source_fields) > 1:
            # Composite target: SQLite can't add a table-level FOREIGN KEY to an existing table -
            # the table is rebuilt.
            await self.table_rebuild.remake_table(model, create_field=field)
            return
        if field.generated and not field.pk:
            # SQLite adds a STORED generated column only to an empty table - the table is rebuilt.
            await self.table_rebuild.remake_table(model, create_field=field)
            return
        qualified_table = self.qualify_table_name(model._meta.db_table, model._meta.schema)
        needs_backfill = False
        if isinstance(field, ForeignKeyFieldInstance):
            key_field_name = field.source_field or field_name
            db_field = model._meta.fields_db_projection.get(key_field_name, key_field_name)
            key_field = model._meta.fields_map[key_field_name]
            foreign_key_field = cast("ForeignKeyFieldInstance[Model]", key_field.reference)
            comment = (
                self.table_comments.get_column_comment_sql(
                    table=qualified_table,
                    column=db_field,
                    comment=foreign_key_field.description,
                )
                if foreign_key_field.description
                else ""
            )

            if self.foreign_key_rebuild.creates_foreign_key(foreign_key_field):
                to_field_name = foreign_key_field.to_field_instance.source_field
                if not to_field_name:
                    to_field_name = foreign_key_field.to_field_instance.model_field_name

                field_definition = self.column_definitions.get_field_sql(
                    db_field=db_field,
                    field_type=self.column_definitions.get_table_column_type(model, key_field),
                    nullable=key_field.null,
                    unique=False,
                    is_pk=key_field.pk,
                    comment="",
                ) + self.column_definitions.get_foreign_key_reference_string(
                    constraint_name=GeneratedNames.get_foreign_key_name(
                        model._meta.db_table,
                        (db_field,),
                        foreign_key_field.related_model._meta.db_table,
                        (to_field_name,),
                    ),
                    db_field=db_field,
                    table=self.qualify_table_name(
                        foreign_key_field.related_model._meta.db_table, foreign_key_field.related_model._meta.schema
                    ),
                    field=to_field_name,
                    on_delete=foreign_key_field.db_on_delete,
                    comment=comment,
                )
            else:
                # No database constraint (creates_foreign_key) means no FK constraint at all - a plain column, as
                # CREATE TABLE gives it (ColumnDefinitions.get_foreign_key_field_definition).
                field_definition = self.column_definitions.get_field_sql(
                    db_field=db_field,
                    field_type=self.column_definitions.get_table_column_type(model, key_field),
                    nullable=key_field.null,
                    unique=False,
                    is_pk=key_field.pk,
                    comment=comment,
                )
            unique_field = key_field.unique and not key_field.pk
        else:
            db_field = model._meta.fields_db_projection[field_name]
            comment = (
                self.table_comments.get_column_comment_sql(
                    table=qualified_table, column=db_field, comment=field.description
                )
                if field.description
                else ""
            )

            # A non-pk GeneratedField never reaches here - the early return above routes it
            # through TableRebuild.remake_table() instead, so field_type only ever needs the plain SQL_TYPE.
            field_type = self.column_definitions.get_table_column_type(model, field)

            # SQLite refuses ADD COLUMN ... NOT NULL without a database default, so a column with
            # only a Python default is added nullable, filled, then rebuilt as NOT NULL.
            needs_backfill = self.column_backfill.needs_added_column_backfill(field)
            field_definition = self.column_definitions.get_field_sql(
                db_field=db_field,
                field_type=field_type,
                nullable=field.null or needs_backfill,
                unique=False,
                is_pk=field.pk,
                comment=comment,
            )
            # The NOT NULL rebuild re-creates the table with the field's UNIQUE inline.
            unique_field = field.unique and not field.pk and not needs_backfill

        if field.has_db_default():
            if hasattr(field.db_default, "get_sql"):
                field_definition += f" DEFAULT {self.column_definitions.get_db_default_sql(field.db_default)}"
            else:
                db_value = self.client.dialect.types.get_db_value(field, field.db_default, model)
                escaped = self.client.dialect.literals.get_literal_sql(db_value)
                field_definition += f" DEFAULT {escaped}"

        await self.run_sql(self.ADD_FIELD_TEMPLATE.format(table=qualified_table, definition=field_definition))

        if unique_field:
            await self.add_constraint(model, UniqueConstraint(fields=(db_field,)))
        if needs_backfill:
            await self.column_backfill.backfill_added_column(model, field, db_field)

    async def add_constraint(self, model: type[Model], constraint: Any) -> None:
        if isinstance(constraint, CheckConstraint):
            await self.table_rebuild.remake_table(model)
            return
        if not self.client.features.supports_unique_constraints:
            return
        if isinstance(constraint, UniqueConstraint):
            self.constraint_statements.check_unique_constraint_supported(constraint)
        constraint_column_names = self.constraint_names.get_fields_to_columns(model, constraint.fields)
        column_constraint = UniqueConstraint(fields=tuple(constraint_column_names), name=constraint.name)
        constraint_name = self.constraint_names.constraint_name_for_model(model, column_constraint)
        # A partial unique constraint is a unique index with a WHERE clause, as on Postgres.
        condition = getattr(constraint, "condition", None)
        if (
            not condition
            and not self.collect_sql
            and self._is_relation_unique_key(model, list(constraint_column_names))
            and await self.constraint_names.get_unique_constraint_names_from_db(
                model._meta.db_table, list(constraint_column_names), model._meta.schema
            )
        ):
            # A table rebuild earlier in the migration already created the unique key a composite
            # one-to-one relation declares, from the model.
            return
        index_sql = self.UNIQUE_INDEX_CREATE_TEMPLATE.format(
            exists="",
            index_type="",
            index_name=self.quote(constraint_name),
            table_name=self.qualify_table_name(model._meta.db_table, model._meta.schema),
            fields=", ".join([self.quote(column_name) for column_name in constraint_column_names]),
            extra=f" WHERE {ConstraintCondition.get_sql(condition, model, self.client)}" if condition else "",
        )
        await self.run_sql(index_sql)

    @staticmethod
    def _is_relation_unique_key(model: type[Model], column_names: list[str]) -> bool:
        """Whether ``column_names`` are the key columns of one of ``model``'s composite one-to-one
        relations - a unique key the relation itself declares."""
        return any(
            isinstance(field, OneToOneFieldInstance)
            and len(field.db_column_names) > 1
            and list(field.db_column_names) == column_names
            for field in model._meta.fields_map.values()
        )

    async def remove_constraint(self, model: type[Model], constraint: Any) -> None:
        if isinstance(constraint, CheckConstraint):
            # A CHECK constraint only ever lives inside the CREATE TABLE body - SQLite has no
            # ALTER TABLE DROP CONSTRAINT at all - so removing it means rebuilding the table
            # without re-emitting this one, same as add_constraint's own rebuild does for adding.
            await self.table_rebuild.remake_table(model, delete_constraint=constraint)
            return
        if not self.client.features.supports_unique_constraints:
            return
        constraint_column_names = self.constraint_names.get_fields_to_columns(model, constraint.fields)
        column_constraint = UniqueConstraint(fields=tuple(constraint_column_names), name=constraint.name)
        if constraint.condition:
            # Always a standalone index under its own name - looking it up by its columns could
            # find an unconditional unique index over the same columns instead.
            await self.run_sql(
                self.DROP_INDEX_TEMPLATE.format(
                    name=self.qualify_table_name(
                        self.constraint_names.constraint_name_for_model(model, column_constraint), model._meta.schema
                    )
                )
            )
            return
        constraint_name = await self.constraint_names.get_constraint_name(model, column_constraint)
        if constraint_name.startswith(SQLITE_AUTOINDEX_PREFIX):
            # A unique constraint inlined into CREATE TABLE is backed by an sqlite_autoindex that
            # can't be dropped - only a rebuild removes it.
            await self.table_rebuild.remake_table(model, delete_constraint=constraint)
            return
        await self.remove_index(
            model,
            Index(fields=tuple(constraint_column_names), name=constraint_name),
        )

    async def rename_constraint(self, model: type[Model], old_constraint: Any, new_constraint: Any) -> None:
        """Renames a constraint: one inlined into CREATE TABLE (every check constraint, a unique
        constraint behind an ``sqlite_autoindex_*``) by rebuilding the table from ``model``; a
        standalone unique index by dropping and creating it under the new name.
        """
        if isinstance(old_constraint, CheckConstraint):
            await self.table_rebuild.remake_table(model)
            return
        if (
            isinstance(old_constraint, UniqueConstraint)
            and not old_constraint.condition
            and self.client.features.supports_unique_constraints
        ):
            old_name = await self.constraint_names.get_constraint_name(model, old_constraint)
            if old_name.startswith(SQLITE_AUTOINDEX_PREFIX):
                await self.table_rebuild.remake_table(model)
                return
        await super().rename_constraint(model, old_constraint, new_constraint)

    @staticmethod
    def get_own_table_indexes(model: type[Model]) -> list[OwnTableIndex]:
        """The model's indexes kept in tables of their own - full-text and spatial ones - which
        outlive a DROP TABLE of the model's table and read it and its columns by name.

        Args:
            model: The model.

        Returns:
            The indexes.
        """
        return [index for index in model._meta.indexes if isinstance(index, OwnTableIndex)]

    async def drop_own_table_indexes(
        self, model: type[Model], indexes: Iterable[OwnTableIndex], table_name: str | None = None
    ) -> None:
        """Drops indexes of a model kept in tables of their own, each only when it exists.

        Args:
            model: The indexed model.
            indexes: The indexes.
            table_name: The indexed table as the database has it now - the model's by default.
        """
        for index in indexes:
            await self.run_sql(
                "\n".join(
                    index.get_drop_sqls(
                        self, table_name or model._meta.db_table, model._meta.get_column_names(index.fields)
                    )
                )
            )

    async def add_index(self, model: type[Model], index: Index, concurrently: bool = False) -> None:
        """Creates an index - SpatiaLite answers each step of a spatial index with 1, or 0 when it
        refused it, which is raised."""
        if not isinstance(index, SpatialiteIndex) or self.collect_sql:
            await super().add_index(model, index, concurrently)
            return
        for step, sql in enumerate(index.get_create_sqls(self, model, safe=False)):
            rows = await self.client.execute_dicts(sql.removesuffix(";"))
            if rows and rows[0][SPATIALITE_INDEX_RESULT_COLUMN] == 1:
                continue
            reason = (
                "SpatiaLite refused to register the column - a row's geometry is of another type, SRID or "
                "dimensions than the field declares"
                if step == 0
                else "SpatiaLite refused to create its R*Tree"
            )
            raise OperationalError(f"{index!r} of {model.__name__} wasn't created: {reason}")

    async def rename_table(self, model: type[Model], old_name: str, new_name: str) -> None:
        """Renames a table - its indexes kept in tables of their own read it by name: dropped under
        the old name first and created again for the new one."""
        if old_name == new_name:
            return
        own_table_indexes = self.get_own_table_indexes(model)
        await self.drop_own_table_indexes(model, own_table_indexes, old_name)
        await super().rename_table(model, old_name, new_name)
        for index in own_table_indexes:
            await self.add_index(model, index)

    async def delete_model(self, model: type[Model]) -> None:
        # No override needed for the trigger cleanup base.delete_model() does after dropping the
        # table: SQLite triggers have no separate backing function object (see add_trigger()
        # above) and DROP TABLE already removes any trigger defined on that table by itself. An
        # index kept in a table of its own outlives the table - dropped first.
        await self.drop_own_table_indexes(model, self.get_own_table_indexes(model))
        schema = model._meta.schema
        for field_name in sorted(model._meta.many_to_many_fields):
            field = cast("ManyToManyFieldInstance[Model]", model._meta.fields_map[field_name])
            await self.run_sql(self.DELETE_TABLE_TEMPLATE.format(table=self.qualify_table_name(field.through, schema)))
        await self.run_sql(
            self.DELETE_TABLE_TEMPLATE.format(table=self.qualify_table_name(model._meta.db_table, schema))
        )

    async def remove_field(self, model: type[Model], field: Field[Any]) -> None:
        if isinstance(field, ManyToManyFieldInstance):
            await self.run_sql(
                self.DELETE_TABLE_TEMPLATE.format(table=self.qualify_table_name(field.through, model._meta.schema))
            )
            return
        if await self._drops_column_in_place(model, field):
            await self.run_sql(
                self.DELETE_FIELD_TEMPLATE.format(
                    table=self.qualify_table_name(model._meta.db_table, model._meta.schema),
                    column=self.quote(model._meta.fields_db_projection[field.model_field_name]),
                )
            )
            return
        await self.table_rebuild.remake_table(model, delete_field=field)

    async def _drops_column_in_place(self, model: type[Model], field: Field[Any]) -> bool:
        """Whether ``ALTER TABLE ... DROP COLUMN`` drops the field's column instead of a table rebuild.
        SQLite refuses a column that is a key, unique, indexed, a foreign key or named by a CHECK, a
        generated column, a trigger or a view - such a column, and any column of a model with a
        CHECK constraint, a generated column or a trigger, goes through the rebuild. Outside
        ``collect_sql`` the database is read too, for the indexes, triggers, views and CHECKs the
        migrations don't know.

        Args:
            model: The model the field is removed from.
            field: The removed field.

        Returns:
            True when the column is dropped in place.
        """
        meta = model._meta
        if (
            not self.client.features.supports_drop_column
            or isinstance(field, ForeignKeyFieldInstance)
            or field.pk
            or field.unique
            or field.index
            or field.generated
            or field.reference is not None
            or field.model_field_name not in meta.fields_db_projection
            or meta.triggers
            # The triggers of an index kept in a table of its own name the table's columns.
            or self.get_own_table_indexes(model)
            # A generated column - an auto-incremented primary key is "generated" too, and harmless.
            or any(other_field.generated and not other_field.pk for other_field in meta.fields_map.values())
        ):
            return False
        for index in meta.indexes:
            if not isinstance(index, Index):
                index = Index(fields=tuple(index))
            if (
                index.expressions
                or isinstance(index, PartialIndex)
                or field.model_field_name in index.include
                or self.table_rebuild.index_references_field(index, field)
            ):
                return False
        for constraint in getattr(meta, ModelOption.CONSTRAINTS, None) or ():
            if not isinstance(constraint, UniqueConstraint) or (
                constraint.condition is not None
                or field.model_field_name in constraint.include
                or self.table_rebuild.constraint_references_field(constraint, field)
            ):
                return False
        if self.collect_sql:
            return True
        return await self._database_allows_drop_column(
            meta.db_table, meta.schema, meta.fields_db_projection[field.model_field_name]
        )

    async def _database_allows_drop_column(self, table_name: str, schema: str | None, column_name: str) -> bool:
        """Whether nothing in the database keeps SQLite from dropping the column: no index over it or
        over an expression or with a WHERE, no foreign key or primary key on it, no CHECK in the
        table, no trigger on the table and no view naming the column.

        Args:
            table_name: The table.
            schema: Its schema.
            column_name: The column.

        Returns:
            True when ``ALTER TABLE ... DROP COLUMN`` can drop it.
        """
        prefix = f"{self.quote(schema)}." if schema else ""
        quoted_table = self.quote(table_name)
        _, columns = await self.client.execute(f"PRAGMA {prefix}table_info({quoted_table})")
        if any(column["name"] == column_name and column["pk"] for column in columns):
            return False
        _, foreign_keys = await self.client.execute(f"PRAGMA {prefix}foreign_key_list({quoted_table})")
        if any(foreign_key["from"] == column_name for foreign_key in foreign_keys):
            return False
        _, indexes = await self.client.execute(f"PRAGMA {prefix}index_list({quoted_table})")
        for index in indexes:
            if index["partial"]:
                return False
            _, index_columns = await self.client.execute(f"PRAGMA {prefix}index_xinfo({self.quote(index['name'])})")
            if any(
                index_column["name"] == column_name or index_column["cid"] == SQLITE_EXPRESSION_INDEX_COLUMN
                for index_column in index_columns
            ):
                return False
        _, schema_objects = await self.client.execute(SQLITE_DROP_COLUMN_BLOCKERS_SQL.format(schema=prefix))
        lowered_column_name = column_name.lower()
        for schema_object in schema_objects:
            definition = (schema_object["sql"] or "").upper()
            if schema_object["type"] == "table":
                if schema_object["tbl_name"] == table_name and SQLITE_CHECK_KEYWORD in definition:
                    return False
            elif schema_object["type"] == "trigger":
                if schema_object["tbl_name"] == table_name:
                    return False
            elif lowered_column_name in definition.lower():
                return False
        return True

    async def alter_field(
        self,
        old_model: type[Model],
        new_model: type[Model],
        field_name: str,
        candidate_referencing_models: Sequence[type[Model]] = (),
    ) -> None:
        old_field = old_model._meta.fields_map[field_name]
        new_field = new_model._meta.fields_map[field_name]
        if self.column_type_changes.keeps_table(old_field, new_field, self.client.dialect):
            # The table stays as it is - only the column's own index and its stored values may change.
            old_indexed = old_field.index and not old_field.pk
            new_indexed = new_field.index and not new_field.pk
            if old_indexed != new_indexed:
                index = Index(fields=(self.table_rebuild.get_remake_column_name(new_field),))
                if new_indexed:
                    await self.add_index(new_model, index)
                else:
                    await self.remove_index(new_model, index)
            await self.column_backfill.rewrite_stored_values(new_model, old_field, new_field)
            return
        await super().alter_field(old_model, new_model, field_name, candidate_referencing_models)

    async def alter_column(self, model: type[Model], old_field: Field[Any], new_field: Field[Any]) -> None:
        # A change to a generated field is validated here, as the base editor does.
        await self._alter_generated_field(model, old_field, new_field)
        old_db_field = self.table_rebuild.get_remake_column_name(old_field)
        new_db_field = self.table_rebuild.get_remake_column_name(new_field)

        if old_db_field != new_db_field and not isinstance(old_field, ForeignKeyFieldInstance):
            # An index kept in a table of its own reads its table's columns by name, and is named
            # after them - the one over the renamed column is dropped under its old name first and
            # created again.
            renamed_column_indexes = [
                index for index in self.get_own_table_indexes(model) if new_field.model_field_name in index.fields
            ]
            for index in renamed_column_indexes:
                old_columns = [
                    old_db_field if column == new_db_field else column
                    for column in model._meta.get_column_names(index.fields)
                ]
                await self.run_sql("\n".join(index.get_drop_sqls(self, model._meta.db_table, old_columns)))
            # RENAME COLUMN also rewrites the other tables' FOREIGN KEY clauses naming the column;
            # a table rebuild alone would leave them pointing at a column that no longer exists.
            qualified_table = self.qualify_table_name(model._meta.db_table, model._meta.schema)
            await self.run_sql(
                self.RENAME_FIELD_TEMPLATE.format(
                    table=qualified_table, old_column=self.quote(old_db_field), new_column=self.quote(new_db_field)
                )
            )
            if self.column_type_changes.only_renames_column(old_field, new_field, self.client.dialect):
                for index in renamed_column_indexes:
                    await self.add_index(model, index)
                return
            old_field = copy(old_field)
            old_field.source_field = new_db_field

        if not self.collect_sql:
            # SQLite keeps a value the new column type can't hold - a 30-character string in a
            # VARCHAR(5), a fraction in an INTEGER - so the data is checked first, as on Postgres.
            await self.column_narrowing_check.check_alter_field_narrowing_data_loss(
                self.qualify_table_name(model._meta.db_table, model._meta.schema),
                old_field,
                new_field,
                self.quote(new_db_field),
            )
        await self.table_rebuild.remake_table(model, alter_fields=[(old_field, new_field)])
