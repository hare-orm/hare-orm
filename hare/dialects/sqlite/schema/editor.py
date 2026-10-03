from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from copy import copy
from typing import Any, cast

from hare.ddl.conditions import ConstraintCondition
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.generated_names import GeneratedNames
from hare.ddl.indexes.index import Index
from hare.ddl.triggers import Trigger
from hare.dialects.base.client.transaction_client import TransactionClient
from hare.dialects.base.dialect import Dialect
from hare.dialects.base.schema.data.referencing_key_columns import ReferencingKeyColumns
from hare.dialects.base.schema.editor import BaseSchemaEditor
from hare.dialects.sqlite.client.sqlite_client import SqliteClient
from hare.dialects.sqlite.constants import (
    SQLITE_AUTOINCREMENT_KEYWORD,
    SQLITE_AUTOINDEX_PREFIX,
    SQLITE_BOOLEAN_TO_TEXT_SQL,
    SQLITE_COMMENT_ESCAPES,
    SQLITE_COPY_REBUILT_TABLE_SEQUENCE_SQL,
    SQLITE_DATE_TO_AWARE_DATETIME_SQL,
    SQLITE_DATE_TO_NAIVE_DATETIME_SQL,
    SQLITE_DATETIME_TO_DATE_SQL,
    SQLITE_DECIMAL_OVERFLOWS_FUNCTION_NAME,
    SQLITE_DEFAULT_FOREIGN_KEYS,
    SQLITE_DISABLE_LEGACY_ALTER_TABLE_SQL,
    SQLITE_ENABLE_LEGACY_ALTER_TABLE_SQL,
    SQLITE_FLOAT_TO_DECIMAL_SQL,
    SQLITE_FOREIGN_KEY_CHECK_SQL,
    SQLITE_INTEGER_TO_DECIMAL_SQL,
    SQLITE_NUMBER_TO_BOOLEAN_SQL,
    SQLITE_NUMBER_TO_INTEGER_SQL,
    SQLITE_PRAGMA_ENABLED_VALUES,
    SQLITE_RAISE_REBUILT_TABLE_SEQUENCE_SQL,
    SQLITE_TEXT_TO_BOOLEAN_SQL,
)
from hare.exceptions import IntegrityError, UnSupportedError
from hare.fields.base.field import Field
from hare.fields.constants import DB_DEFAULT_NOT_SET
from hare.fields.data.boolean import BooleanField
from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.data.numeric.float_field import FloatField
from hare.fields.data.numeric.int_field import IntField
from hare.fields.data.temporal.date_field import DateField
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.text.char_field import CharField
from hare.fields.data.text.text_field import TextField
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.models import Model
from hare.models.enums import ModelOption
from hare.utils import Timezone


class SqliteSchemaEditor(BaseSchemaEditor):
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

    async def _run_sql(self, sql: str) -> None:
        """Runs DDL. In atomic mode each statement goes through ``execute()`` - ``executescript()``
        issues an implicit COMMIT. In ``collect_sql`` mode the SQL is only collected.
        """
        if self.collect_sql:
            await super()._run_sql(sql)
            return
        if self.atomic_migration or isinstance(self.client, TransactionClient):
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

    def _get_table_comment_sql(self, table: str, comment: str) -> str:
        return f" /* {comment.translate(SQLITE_COMMENT_ESCAPES)} */"

    def _get_column_comment_sql(self, table: str, column: str, comment: str) -> str:
        return f" /* {comment.translate(SQLITE_COMMENT_ESCAPES)} */"

    async def add_field(self, model, field_name: str) -> None:
        field = model._meta.fields_map[field_name]
        if isinstance(field, ManyToManyFieldInstance):
            table_string = self._get_m2m_table_definition(model, field)
            if table_string:
                await self._run_sql(table_string)
            return
        if isinstance(field, ForeignKeyFieldInstance) and len(field.source_fields) > 1:
            # Composite target: SQLite can't add a table-level FOREIGN KEY to an existing table -
            # the table is rebuilt.
            await self._remake_table(model, create_field=field)
            return
        if field.generated and not field.pk:
            # SQLite adds a STORED generated column only to an empty table - the table is rebuilt.
            await self._remake_table(model, create_field=field)
            return
        qualified_table = self._qualify_table_name(model._meta.db_table, model._meta.schema)
        needs_backfill = False
        if isinstance(field, ForeignKeyFieldInstance):
            key_field_name = field.source_field or field_name
            db_field = model._meta.fields_db_projection.get(key_field_name, key_field_name)
            key_field = model._meta.fields_map[key_field_name]
            fk_field = cast("ForeignKeyFieldInstance[Model]", key_field.reference)
            comment = (
                self._get_column_comment_sql(
                    table=qualified_table,
                    column=db_field,
                    comment=fk_field.description,
                )
                if fk_field.description
                else ""
            )

            if self.creates_foreign_key(fk_field):
                to_field_name = fk_field.to_field_instance.source_field
                if not to_field_name:
                    to_field_name = fk_field.to_field_instance.model_field_name

                field_definition = self._get_field_sql(
                    db_field=db_field,
                    field_type=key_field.get_column_type(self.client.dialect),
                    nullable=key_field.null,
                    unique=False,
                    is_pk=key_field.pk,
                    comment="",
                ) + self._get_fk_reference_string(
                    constraint_name=GeneratedNames.get_foreign_key_name(
                        model._meta.db_table, (db_field,), fk_field.related_model._meta.db_table, (to_field_name,)
                    ),
                    db_field=db_field,
                    table=self._qualify_table_name(
                        fk_field.related_model._meta.db_table, fk_field.related_model._meta.schema
                    ),
                    field=to_field_name,
                    on_delete=fk_field.db_on_delete,
                    comment=comment,
                )
            else:
                # No database constraint (creates_foreign_key) means no FK constraint at all - a plain column, as
                # CREATE TABLE gives it (BaseSchemaEditor._get_fk_field_definition).
                field_definition = self._get_field_sql(
                    db_field=db_field,
                    field_type=key_field.get_column_type(self.client.dialect),
                    nullable=key_field.null,
                    unique=False,
                    is_pk=key_field.pk,
                    comment=comment,
                )
            unique_field = key_field.unique and not key_field.pk
        else:
            db_field = model._meta.fields_db_projection[field_name]
            comment = (
                self._get_column_comment_sql(table=qualified_table, column=db_field, comment=field.description)
                if field.description
                else ""
            )

            # A non-pk GeneratedField never reaches here - the early return above routes it
            # through _remake_table() instead, so field_type only ever needs the plain SQL_TYPE.
            field_type = field.get_column_type(self.client.dialect)

            # SQLite refuses ADD COLUMN ... NOT NULL without a database default, so a column with
            # only a Python default is added nullable, filled, then rebuilt as NOT NULL.
            needs_backfill = self._needs_added_column_backfill(field)
            field_definition = self._get_field_sql(
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
                field_definition += f" DEFAULT {field.db_default.get_sql(self.client.dialect)}"
            else:
                db_val = self.client.dialect.types.get_db_value(field, field.db_default, model)
                escaped = self.client.dialect.get_literal_sql(db_val)
                field_definition += f" DEFAULT {escaped}"

        await self._run_sql(self.ADD_FIELD_TEMPLATE.format(table=qualified_table, definition=field_definition))

        if unique_field:
            await self.add_constraint(model, UniqueConstraint(fields=(db_field,)))
        if needs_backfill:
            await self._backfill_added_column(model, field, db_field)

    async def _set_added_column_not_null(self, model, db_field: str) -> None:
        # No ALTER COLUMN ... SET NOT NULL on SQLite - the rebuild takes the column's NOT NULL
        # (and every other property) from the model's current metadata.
        await self._remake_table(model, added_column_name=db_field)

    async def add_constraint(self, model, constraint) -> None:
        if isinstance(constraint, CheckConstraint):
            await self._remake_table(model)
            return
        if not self.client.dialect.supports_unique_constraints:
            return
        if isinstance(constraint, UniqueConstraint):
            self._check_unique_constraint_supported(constraint)
        constraint_column_names = self._get_fields_to_columns(model, constraint.fields)
        column_constraint = UniqueConstraint(fields=tuple(constraint_column_names), name=constraint.name)
        constraint_name = self._constraint_name_for_model(model, column_constraint)
        # A partial unique constraint is a unique index with a WHERE clause, as on Postgres.
        condition = getattr(constraint, "condition", None)
        if (
            not condition
            and not self.collect_sql
            and self._is_relation_unique_key(model, list(constraint_column_names))
            and await self._get_unique_constraint_names_from_db(
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
            table_name=self._qualify_table_name(model._meta.db_table, model._meta.schema),
            fields=", ".join([self.quote(f) for f in constraint_column_names]),
            extra=f" WHERE {ConstraintCondition.get_sql(condition, model, self.client)}" if condition else "",
        )
        await self._run_sql(index_sql)

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

    async def _get_index_names(self, table_name: str, schema: str | None) -> set[str] | None:
        prefix = f"{self.quote(schema)}." if schema else ""
        _, indexes = await self.client.execute(f"PRAGMA {prefix}index_list({self.quote(table_name)})")
        return {index["name"] for index in indexes}

    async def _get_unique_constraint_names_from_db(
        self, table_name: str, column_names: list[str], schema: str | None = None
    ) -> list[str]:
        """Use PRAGMA index_list + PRAGMA index_info to find unique index names."""
        _, indexes = await self.client.execute(f'PRAGMA index_list("{table_name}")')
        result: list[str] = []
        for idx in indexes:
            if not idx["unique"]:
                continue
            idx_name = idx["name"]
            _, columns = await self.client.execute(f'PRAGMA index_info("{idx_name}")')
            col_names = [col["name"] for col in columns]
            if col_names == column_names:
                result.append(idx_name)
        return result

    async def remove_constraint(self, model, constraint) -> None:
        if isinstance(constraint, CheckConstraint):
            # A CHECK constraint only ever lives inside the CREATE TABLE body - SQLite has no
            # ALTER TABLE DROP CONSTRAINT at all - so removing it means rebuilding the table
            # without re-emitting this one, same as add_constraint's own rebuild does for adding.
            await self._remake_table(model, delete_constraint=constraint)
            return
        if not self.client.dialect.supports_unique_constraints:
            return
        constraint_column_names = self._get_fields_to_columns(model, constraint.fields)
        column_constraint = UniqueConstraint(fields=tuple(constraint_column_names), name=constraint.name)
        if constraint.condition:
            # Always a standalone index under its own name - looking it up by its columns could
            # find an unconditional unique index over the same columns instead.
            await self._run_sql(
                self.DROP_INDEX_TEMPLATE.format(
                    name=self._qualify_table_name(
                        self._constraint_name_for_model(model, column_constraint), model._meta.schema
                    )
                )
            )
            return
        constraint_name = await self._get_constraint_name(model, column_constraint)
        if constraint_name.startswith(SQLITE_AUTOINDEX_PREFIX):
            # A unique constraint inlined into CREATE TABLE is backed by an sqlite_autoindex that
            # can't be dropped - only a rebuild removes it.
            await self._remake_table(model, delete_constraint=constraint)
            return
        await self.remove_index(
            model,
            Index(fields=tuple(constraint_column_names), name=constraint_name),
        )

    async def _rename_generated_index(self, model, old_index, new_index, is_constraint) -> None:
        """SQLite has no ALTER INDEX ... RENAME - a standalone index is dropped and re-created
        under its new name. A unique constraint inlined into CREATE TABLE is backed by an
        sqlite_autoindex_* that has no name of its own to change, so it is left as is."""
        if not self.collect_sql:
            rows = await self.client.execute_dicts(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?", [old_index.name]
            )
            if not rows:
                return
        elif is_constraint:
            return
        await self._run_sql(self.DROP_INDEX_IF_EXISTS_TEMPLATE.format(name=self.quote(old_index.name)))
        partial_unique_constraint = self._get_partial_unique_constraint_named(model, new_index.name)
        if is_constraint and partial_unique_constraint is not None:
            await self.add_constraint(model, partial_unique_constraint)
            return
        await self.add_index(model, new_index)

    def _get_partial_unique_constraint_named(self, model, index_name: str) -> UniqueConstraint | None:
        """The model's partial unique constraint whose index has the given name.

        Args:
            model: The model.
            index_name: The name of the constraint's index.

        Returns:
            The ``condition=`` UniqueConstraint, or None when no such constraint has that name.
        """
        for constraint in getattr(model._meta, ModelOption.CONSTRAINTS, None) or ():
            if not isinstance(constraint, UniqueConstraint) or not constraint.condition:
                continue
            column_constraint = UniqueConstraint(
                fields=tuple(self._get_fields_to_columns(model, constraint.fields)), name=constraint.name
            )
            if self._constraint_name_for_model(model, column_constraint) == index_name:
                return constraint
        return None

    async def rename_constraint(self, model, old_constraint, new_constraint) -> None:
        """Renames a constraint: one inlined into CREATE TABLE (every check constraint, a unique
        constraint behind an ``sqlite_autoindex_*``) by rebuilding the table from ``model``; a
        standalone unique index by dropping and creating it under the new name.
        """
        if isinstance(old_constraint, CheckConstraint):
            await self._remake_table(model)
            return
        if (
            isinstance(old_constraint, UniqueConstraint)
            and not old_constraint.condition
            and self.client.dialect.supports_unique_constraints
        ):
            old_name = await self._get_constraint_name(model, old_constraint)
            if old_name.startswith(SQLITE_AUTOINDEX_PREFIX):
                await self._remake_table(model)
                return
        await super().rename_constraint(model, old_constraint, new_constraint)

    async def _run_ddl_statement(self, sql: str) -> None:
        """Runs one DDL statement whole - ``_run_sql()`` would split a ``CREATE TRIGGER ... BEGIN ...;
        END`` at its inner semicolons.
        """
        if self.collect_sql:
            self.collected_sql.append(sql)
            return
        await self.client.execute(sql)

    def get_trigger_create_sqls(self, model, trigger: Trigger, safe: bool = False) -> list[str]:
        if trigger.for_each == "STATEMENT":
            raise UnSupportedError(
                f"STATEMENT-level triggers are not supported on {self.client.dialect.name}; "
                "SQLite triggers always fire per row."
            )
        if trigger.deferrable:
            raise UnSupportedError(
                f"CONSTRAINT TRIGGER (deferrable=True) is not supported on {self.client.dialect.name}; "
                "it's Postgres-only."
            )
        table = self._qualify_table_name(model._meta.db_table, model._meta.schema)
        when_clause = f"WHEN ({trigger.when})\n" if trigger.when else ""
        template = self.TRIGGER_CREATE_IF_NOT_EXISTS_TEMPLATE if safe else self.TRIGGER_CREATE_TEMPLATE
        return [
            template.format(
                trigger_name=self.quote(trigger.name),
                timing=trigger.timing,
                on=trigger.on,
                table=table,
                when_clause=when_clause,
                body=trigger.body,
            )
        ]

    async def add_trigger(self, model, trigger: Trigger) -> None:
        for statement in self.get_trigger_create_sqls(model, trigger):
            await self._run_ddl_statement(statement)

    async def remove_trigger(self, model, trigger: Trigger) -> None:
        await self._run_sql(self.TRIGGER_DROP_TEMPLATE.format(trigger_name=self.quote(trigger.name)))

    async def delete_model(self, model) -> None:
        # No override needed for the trigger cleanup base.delete_model() does after dropping the
        # table: SQLite triggers have no separate backing function object (see add_trigger()
        # above) and DROP TABLE already removes any trigger defined on that table by itself.
        schema = model._meta.schema
        for field_name in sorted(model._meta.m2m_fields):
            field = cast("ManyToManyFieldInstance[Model]", model._meta.fields_map[field_name])
            await self._run_sql(
                self.DELETE_TABLE_TEMPLATE.format(table=self._qualify_table_name(field.through, schema))
            )
        await self._run_sql(
            self.DELETE_TABLE_TEMPLATE.format(table=self._qualify_table_name(model._meta.db_table, schema))
        )

    async def remove_field(self, model, field) -> None:
        if isinstance(field, ManyToManyFieldInstance):
            await self._run_sql(
                self.DELETE_TABLE_TEMPLATE.format(table=self._qualify_table_name(field.through, model._meta.schema))
            )
            return
        await self._remake_table(model, delete_field=field)

    async def _alter_composite_relation_index(self, model, old_field, new_field) -> None:
        """Does nothing - altering the key columns already rebuilt the table with the new
        definition's indexes.

        Args:
            model: The model rendered from the target state.
            old_field: The relation's previous definition.
            new_field: The relation's new definition.
        """

    def get_decimal_overflow_predicate_sql(self, quoted_column: str, max_digits: int, decimal_places: int) -> str:
        # Decimals are stored as text and CAST(... AS NUMERIC) gives a double - the value is read
        # as its exact decimal text instead.
        return f"{SQLITE_DECIMAL_OVERFLOWS_FUNCTION_NAME}({quoted_column}, {max_digits}, {decimal_places})"

    async def _alter_field(self, model, old_field, new_field) -> None:
        # A change to a generated field is validated here, as the base editor does.
        await self._alter_generated_field(model, old_field, new_field)
        old_db_field = self._get_remake_column_name(old_field)
        new_db_field = self._get_remake_column_name(new_field)

        if old_db_field != new_db_field and not isinstance(old_field, ForeignKeyFieldInstance):
            # RENAME COLUMN also rewrites the other tables' FOREIGN KEY clauses naming the column;
            # a table rebuild alone would leave them pointing at a column that no longer exists.
            qualified_table = self._qualify_table_name(model._meta.db_table, model._meta.schema)
            await self._run_sql(
                self.RENAME_FIELD_TEMPLATE.format(
                    table=qualified_table, old_column=self.quote(old_db_field), new_column=self.quote(new_db_field)
                )
            )
            if self.only_renames_column(old_field, new_field, self.client.dialect):
                return
            old_field = copy(old_field)
            old_field.source_field = new_db_field

        if not self.collect_sql:
            # SQLite keeps a value the new column type can't hold - a 30-character string in a
            # VARCHAR(5), a fraction in an INTEGER - so the data is checked first, as on Postgres.
            await self._check_alter_field_narrowing_data_loss(
                self._qualify_table_name(model._meta.db_table, model._meta.schema),
                old_field,
                new_field,
                self.quote(new_db_field),
            )
        await self._remake_table(model, alter_fields=[(old_field, new_field)])

    @classmethod
    def only_renames_column(cls, old_field: Field[Any], new_field: Field[Any], dialect: Dialect) -> bool:
        """Whether altering a field only renames its column - ``RENAME COLUMN`` does it; any other
        change rebuilds the table.

        Args:
            old_field: The field before the change.
            new_field: The field after it.
            dialect: The database's dialect.

        Returns:
            True when the column's name is all that changes.
        """
        return (
            not isinstance(old_field, ForeignKeyFieldInstance)
            and cls._get_remake_column_name(old_field) != cls._get_remake_column_name(new_field)
            and old_field.null == new_field.null
            and old_field.unique == new_field.unique
            and old_field.index == new_field.index
            and getattr(old_field, "db_default", DB_DEFAULT_NOT_SET)
            == getattr(new_field, "db_default", DB_DEFAULT_NOT_SET)
            and old_field.get_column_type(dialect) == new_field.get_column_type(dialect)
            and not old_field.pk
            and not new_field.pk
        )

    @classmethod
    def rewrites_table_on_alter(
        cls, old_model: type[Model], new_model: type[Model], field_name: str, dialect: Dialect
    ) -> bool:
        """SQLite alters a column only by rebuilding its table - everything but a pure rename of a
        plain column, and a many-to-many relation, whose through table holds its columns.

        Args:
            old_model: The model rendered from the state before the change.
            new_model: The model rendered from the state after it.
            field_name: The altered field's name.
            dialect: The database's dialect.

        Returns:
            True when the table is rebuilt.
        """
        altered_columns = cls.get_altered_columns(old_model, new_model, field_name)
        if not altered_columns:
            return False
        old_field = old_model._meta.fields_map[field_name]
        new_field = new_model._meta.fields_map[field_name]
        return not cls.only_renames_column(old_field, new_field, dialect)

    @staticmethod
    def _get_remake_value_conversion_sql(old_field: Field[Any], new_field: Field[Any], quoted_column: str) -> str:
        """The expression copying a column's values into a rebuilt table whose field changed type.

        Args:
            old_field: The field's previous definition.
            new_field: The field's new definition.
            quoted_column: The quoted column of the table being replaced.

        Returns:
            An expression converting each stored value to the new field's storage format, or the
            column itself when the format stays the same.
        """
        number_field_classes = (IntField, FloatField, DecimalField)
        text_field_classes = (CharField, TextField)
        template = None
        template_arguments: dict[str, Any] = {}
        if isinstance(new_field, BooleanField) and not isinstance(old_field, BooleanField):
            if isinstance(old_field, number_field_classes):
                template = SQLITE_NUMBER_TO_BOOLEAN_SQL
            elif isinstance(old_field, text_field_classes):
                template = SQLITE_TEXT_TO_BOOLEAN_SQL
        elif isinstance(old_field, BooleanField) and isinstance(new_field, text_field_classes):
            template = SQLITE_BOOLEAN_TO_TEXT_SQL
        elif isinstance(new_field, IntField) and isinstance(old_field, (FloatField, DecimalField)):
            template = SQLITE_NUMBER_TO_INTEGER_SQL
        elif isinstance(new_field, DecimalField) and isinstance(old_field, (IntField, BooleanField)):
            template = SQLITE_INTEGER_TO_DECIMAL_SQL
            template_arguments["fraction"] = (
                f" || '.{'0' * new_field.decimal_places}'" if new_field.decimal_places else ""
            )
        elif isinstance(new_field, DecimalField) and isinstance(old_field, FloatField):
            template = SQLITE_FLOAT_TO_DECIMAL_SQL
            template_arguments["decimal_places"] = new_field.decimal_places
        elif isinstance(new_field, DateField) and isinstance(old_field, DatetimeField):
            template = SQLITE_DATETIME_TO_DATE_SQL
        elif isinstance(new_field, DatetimeField) and isinstance(old_field, DateField):
            template = (
                SQLITE_DATE_TO_AWARE_DATETIME_SQL if Timezone.get_use_tz() else SQLITE_DATE_TO_NAIVE_DATETIME_SQL
            )
        if template is None:
            return quoted_column
        return template.format(column=quoted_column, **template_arguments)

    async def _alter_referenced_field(
        self,
        model: type[Model],
        old_field: Field[Any],
        new_field: Field[Any],
        referencing_key_columns: list[ReferencingKeyColumns],
    ) -> None:
        """Rebuilds the altered table, then every table whose key columns reference it - a rebuilt
        table takes its key column types from the referenced columns' current definitions."""
        await self._alter_field(model, old_field, new_field)
        remade_model_keys = {(model._meta.app, model.__name__)}
        for referencing in referencing_key_columns:
            if isinstance(referencing.relation_field, ManyToManyFieldInstance):
                await self._rebuild_m2m_through_foreign_keys(referencing.model, referencing.relation_field)
                continue
            referencing_model_key = (referencing.model._meta.app, referencing.model.__name__)
            if referencing_model_key in remade_model_keys:
                continue
            remade_model_keys.add(referencing_model_key)
            await self._remake_table(referencing.model)

    async def _restore_relation_foreign_keys(self, model: type[Model], relation_field: Field[Any]) -> None:
        """No-op - the table rebuild that altered the key column already re-created them."""

    async def _rebuild_fk_foreign_keys(self, model: type[Model], fk_field: ForeignKeyFieldInstance[Model]) -> None:
        """SQLite can't replace a constraint in place - the table is rebuilt from the model's
        current metadata, which already carries the field's ON DELETE action."""
        await self._remake_table(model)

    async def _rebuild_m2m_through_foreign_keys(
        self, model: type[Model], field: ManyToManyFieldInstance[Model]
    ) -> None:
        """Rebuilds the auto-managed through table from the field's current metadata, keeping
        its rows."""
        schema = model._meta.schema
        rebuilt_field = copy(field)
        rebuilt_field.through = f"new__{field.through}"
        # The indexes are created after the rename below, so they get the real table's name.
        rebuilt_field.unique = False
        rebuilt_field.index = False
        definition = self._get_m2m_table_definition(model, rebuilt_field)
        if definition is None:
            return
        await self._run_sql(definition)
        qualified_old = self._qualify_table_name(field.through, schema)
        qualified_new = self._qualify_table_name(rebuilt_field.through, schema)
        columns = ", ".join(self.quote(key) for key in (*field.backward_keys, *field.forward_keys))
        await self._run_sql(f"INSERT INTO {qualified_new} ({columns}) SELECT {columns} FROM {qualified_old}")  # nosec B608
        await self._replace_table(field.through, rebuilt_field.through, schema, fields=())
        if field.unique:
            await self._run_sql(
                self._get_unique_index_sql(field.through, [*field.backward_keys, *field.forward_keys], schema=schema)
            )
        for index_keys in field.get_through_index_keys():
            await self._run_sql(self._get_table_index_sql(field.through, index_keys, schema=schema))

    def _uses_autoincrement(self, fields: Iterable[Field[Any]]) -> bool:
        """Whether a table built from ``fields`` has an AUTOINCREMENT primary key.

        Args:
            fields: The fields of the table's columns.

        Returns:
            True when its generated primary key never reuses ids.
        """
        for field in fields:
            if field.pk and field.generated and not isinstance(field, ForeignKeyFieldInstance):
                generated_sql = field.get_generated_sql(self.client.dialect) or ""
                return SQLITE_AUTOINCREMENT_KEYWORD in generated_sql.upper()
        return False

    async def _replace_table(
        self, table_name: str, rebuilt_table_name: str, schema: str | None, fields: Iterable[Field[Any]]
    ) -> None:
        """Drops a table and renames its rebuilt copy into its place - an AUTOINCREMENT primary
        key goes on from the highest id the replaced table ever issued, and the rename leaves
        other tables' references to the table as they are (``legacy_alter_table``).

        Args:
            table_name: The table being replaced.
            rebuilt_table_name: The already filled copy taking its place.
            schema: The tables' schema.
            fields: The fields of the rebuilt table's columns.
        """
        keeps_sequence = self._uses_autoincrement(fields)
        qualified_table = self._qualify_table_name(table_name, schema)
        qualified_rebuilt_table = self._qualify_table_name(rebuilt_table_name, schema)
        if keeps_sequence:
            sequence_table_names = {
                "old_table": self.client.dialect.get_string_literal_sql(table_name),
                "new_table": self.client.dialect.get_string_literal_sql(rebuilt_table_name),
            }
            await self._run_sql(SQLITE_RAISE_REBUILT_TABLE_SEQUENCE_SQL.format(**sequence_table_names))
            await self._run_sql(SQLITE_COPY_REBUILT_TABLE_SEQUENCE_SQL.format(**sequence_table_names))
        await self._run_sql(f"DROP TABLE {qualified_table}")
        await self._run_sql(SQLITE_ENABLE_LEGACY_ALTER_TABLE_SQL)
        try:
            await self._run_sql(f"ALTER TABLE {qualified_rebuilt_table} RENAME TO {qualified_table}")
        finally:
            await self._run_sql(SQLITE_DISABLE_LEGACY_ALTER_TABLE_SQL)
