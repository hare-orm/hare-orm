from __future__ import annotations

import inspect
import re
from collections.abc import AsyncGenerator, Collection, Iterable, Sequence
from contextlib import asynccontextmanager
from copy import copy
from typing import Any, cast

from hare.ddl.conditions import ConstraintCondition
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.foreign_key_constraint import ForeignKeyConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.enums import GeneratedNamePrefix
from hare.ddl.generated_names import GeneratedNames
from hare.ddl.indexes.index import Index
from hare.ddl.table_options import TableOptions
from hare.ddl.triggers import Trigger
from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.constants import (
    CHECK_CONSTRAINT_CREATE_TEMPLATE as SHARED_CHECK_CONSTRAINT_CREATE_TEMPLATE,
    FK_TEMPLATE as SHARED_FK_TEMPLATE,
    FOREIGN_KEY_CONSTRAINT_CREATE_TEMPLATE as SHARED_FOREIGN_KEY_CONSTRAINT_CREATE_TEMPLATE,
    GENERATED_PK_TEMPLATE as SHARED_GENERATED_PK_TEMPLATE,
    PRIMARY_KEY_CONSTRAINT_CREATE_TEMPLATE as SHARED_PRIMARY_KEY_CONSTRAINT_CREATE_TEMPLATE,
    UNIQUE_CONSTRAINT_CREATE_TEMPLATE as SHARED_UNIQUE_CONSTRAINT_CREATE_TEMPLATE,
)
from hare.dialects.base.dialect import Dialect
from hare.dialects.base.schema.data.model_sql_data import ModelSqlData
from hare.dialects.base.schema.data.referencing_key_columns import ReferencingKeyColumns
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.fields.base.field import Field
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.enums import NarrowedValueSource, OnDelete
from hare.fields.generated import GeneratedField
from hare.fields.narrowing.decimal_digits_limit import DecimalDigitsLimit
from hare.fields.narrowing.narrowing_limit import NarrowingLimit
from hare.fields.narrowing.text_length_limit import TextLengthLimit
from hare.fields.relations.fields.backward_fk_relation import BackwardFKRelation
from hare.fields.relations.fields.backward_one_to_one_relation import BackwardOneToOneRelation
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.migrations.exceptions import FieldNarrowingDataLossError, ForeignKeyTargetChangeError
from hare.models import Model
from hare.models.deletion.protect_constraint_deferral import ProtectConstraintDeferral
from hare.models.enums import ModelOption
from hare.models.meta_info import MetaInfo
from hare.query.expressions import Q


class BaseSchemaEditor:
    """Writes the DDL of the models and of each migration operation: tables, fields, indexes,
    constraints, triggers, schemas, extensions and table rebuilds. A dialect subclasses it."""

    TABLE_CREATE_TEMPLATE = "CREATE {prefix}TABLE {exists}{table_name} ({fields}){extra}{comment};"
    FIELD_TEMPLATE = "{name} {type}{nullable}{unique}{primary}{default}{comment}"
    INDEX_CREATE_TEMPLATE = "CREATE {index_type}INDEX {exists}{index_name} ON {table_name} ({fields}){extra};"
    UNIQUE_INDEX_CREATE_TEMPLATE = INDEX_CREATE_TEMPLATE.replace("INDEX", "UNIQUE INDEX")
    UNIQUE_CONSTRAINT_CREATE_TEMPLATE = SHARED_UNIQUE_CONSTRAINT_CREATE_TEMPLATE
    PRIMARY_KEY_CONSTRAINT_CREATE_TEMPLATE = SHARED_PRIMARY_KEY_CONSTRAINT_CREATE_TEMPLATE
    CHECK_CONSTRAINT_CREATE_TEMPLATE = SHARED_CHECK_CONSTRAINT_CREATE_TEMPLATE
    GENERATED_PK_TEMPLATE = SHARED_GENERATED_PK_TEMPLATE
    FK_TEMPLATE = SHARED_FK_TEMPLATE
    FOREIGN_KEY_CONSTRAINT_CREATE_TEMPLATE = SHARED_FOREIGN_KEY_CONSTRAINT_CREATE_TEMPLATE
    M2M_TABLE_TEMPLATE = "CREATE TABLE {exists}{table_name} ({fields}){extra}{comment};"
    RENAME_TABLE_TEMPLATE = "ALTER TABLE {old_table} RENAME TO {new_table}"
    DELETE_TABLE_TEMPLATE = "DROP TABLE {table} CASCADE"
    ADD_FIELD_TEMPLATE = "ALTER TABLE {table} ADD COLUMN {definition}"

    ALTER_FIELD_TEMPLATE = "ALTER TABLE {table} {changes}"
    # The column and name placeholders have no quotes - the caller passes quoted values.
    RENAME_FIELD_TEMPLATE = "ALTER TABLE {table} RENAME COLUMN {old_column} TO {new_column}"
    ALTER_FIELD_NULL_TEMPLATE = "ALTER COLUMN {column} DROP NOT NULL"
    ALTER_FIELD_NOT_NULL_TEMPLATE = "ALTER COLUMN {column} SET NOT NULL"
    ALTER_FIELD_TYPE_TEMPLATE = "ALTER COLUMN {column} SET DATA TYPE {sql_type}"
    ALTER_FIELD_SET_DEFAULT_TEMPLATE = "ALTER COLUMN {column} SET DEFAULT {default}"
    ALTER_FIELD_DROP_DEFAULT_TEMPLATE = "ALTER COLUMN {column} DROP DEFAULT"

    DELETE_FIELD_TEMPLATE = "ALTER TABLE {table} DROP COLUMN {column} CASCADE"

    DELETE_CONSTRAINT_TEMPLATE = "ALTER TABLE {table} DROP CONSTRAINT {name}"
    ADD_CONSTRAINT_TEMPLATE = "ALTER TABLE {table} ADD {constraint}"
    # DROP INDEX/ALTER INDEX name no table: {name}/{old_name} are passed schema-qualified,
    # {new_name} as a plain quoted name.
    DROP_INDEX_TEMPLATE = "DROP INDEX {name}"
    #: How an index is renamed in place; None drops it and creates it again under the new name.
    RENAME_INDEX_TEMPLATE: str | None = None
    #: The same, doing nothing when the index doesn't exist; None renames it with rename_index().
    RENAME_INDEX_IF_EXISTS_TEMPLATE: str | None = None
    RENAME_CONSTRAINT_TEMPLATE: str | None = "ALTER TABLE {table} RENAME CONSTRAINT {old_name} TO {new_name}"

    def __init__(self, connection: DatabaseClient, atomic: bool = True, collect_sql: bool = False) -> None:
        self.client = connection
        self.atomic = atomic
        self.atomic_migration = connection.features.can_rollback_ddl and atomic
        self.collect_sql = collect_sql
        self.collected_sql: list[str] = []

    async def _run_sql(self, sql: str) -> None:
        """Execute DDL SQL. Subclasses may override for backend-specific handling.

        If ``collect_sql`` is True, append the SQL to ``collected_sql`` instead
        of executing it.
        """
        if self.collect_sql:
            self.collected_sql.append(sql)
            return
        ProtectConstraintDeferral.forget_constraint_names()
        await self.client.execute_script(sql)

    @asynccontextmanager
    async def constraint_checking_disabled(self) -> AsyncGenerator[None]:
        """No-op by default - only SQLite's own table-rebuild approach (SqliteSchemaEditor's
        override) needs this. Postgres supports a real ALTER TABLE for everything this editor
        does, so it never has to drop and recreate a table just to change one column."""
        yield

    async def check_constraints(self) -> None:
        """Verifies constraints left unchecked by ``constraint_checking_disabled()``. No-op by
        default - only a backend that actually disables enforcement needs it."""
        return None

    def quote(self, name: str) -> str:
        """Quotes a name for this connection's dialect."""
        return self.client.dialect.quote_identifier(name)

    def _qualify_table_name(self, table_name: str, schema: str | None = None) -> str:
        """Quotes a table name, with its schema where the dialect has schemas."""
        return self.client.dialect.qualify_table_name(table_name, schema)

    def _format_index_fields(
        self,
        field_names: Sequence[str],
        opclasses: Sequence[str] | None = None,
        column_names: Collection[str] = (),
        orders: Sequence[str] | None = None,
    ) -> str:
        """Renders an index's column list - a column quoted, a rendered expression as it is.

        Args:
            field_names: The indexed columns or rendered expressions, in order.
            opclasses: The operator class of each, if any.
            column_names: The table's real columns - one of them is always quoted, whatever
                characters its name has.
            orders: ``"DESC"`` or ``""`` for each, if any.

        Returns:
            The comma-separated list.
        """
        formatted = []
        for i, field in enumerate(field_names):
            is_expression = field not in column_names and GeneratedNames.is_index_expression(field)
            column = field if is_expression else self.quote(field)
            opclass = opclasses[i] if opclasses and i < len(opclasses) else ""
            # An opclass name is quoted like any identifier.
            order = orders[i] if orders and i < len(orders) else ""
            key = f"{column} {self.quote(opclass)}" if opclass else column
            formatted.append(f"{key} {order}" if order else key)
        return ", ".join(formatted)

    def _get_table_comment_sql(self, table: str, comment: str) -> str:
        # Databases have their own way of supporting comments for table level
        raise NotImplementedError()  # pragma: nocoverage

    def _get_column_comment_sql(self, table: str, column: str, comment: str) -> str:
        # Databases have their own way of supporting comments for column level
        raise NotImplementedError()  # pragma: nocoverage

    async def _alter_column_comment(self, model: type[Model], old_field: Field[Any], new_field: Field[Any]) -> None:
        """Alter column comment. Override in backends that support column comments."""
        pass

    async def alter_table_comment(self, model: type[Model]) -> None:
        """Sets the table comment to the model's ``table_description`` (removes it when empty).
        No-op by default - overridden by backends with separately stored table comments.

        Args:
            model: The model, rendered with its new description.
        """
        return None

    def _table_generate_extra(self, table: str) -> str:
        return ""

    def _post_table_hook(self) -> str:
        return ""

    def _get_field_sql(
        self,
        db_field: str,
        field_type: str,
        nullable: bool,
        unique: bool,
        is_pk: bool,
        comment: str,
        default: str = "",
    ) -> str:
        """Returns one column's definition inside ``CREATE TABLE``/``ADD COLUMN``.

        Args:
            db_field: The column's name.
            field_type: The column's SQL type, with any clause that follows it
                (``GENERATED ALWAYS AS (...)``).
            nullable: Whether the column accepts NULL.
            unique: Whether the column carries an unnamed ``UNIQUE`` marker - never for a
                primary key, which is unique already, nor on a database without unique
                constraints (``Dialect.supports_unique_constraints``).
            is_pk: Whether the column is the table's single-column primary key.
            comment: The inline column comment, used where the connection supports inline comments.
            default: The ``DEFAULT`` clause, with its leading space, or an empty string.

        Returns:
            The column definition.
        """
        return self.FIELD_TEMPLATE.format(
            name=self.quote(db_field),
            type=field_type,
            nullable="" if nullable else " NOT NULL",
            unique=" UNIQUE" if unique and not is_pk and self.client.dialect.supports_unique_constraints else "",
            primary=" PRIMARY KEY" if is_pk else "",
            default=default,
            comment=comment if self.client.features.inline_comments else "",
        ).strip()

    def _get_non_pk_generated_field_sql(
        self, field_object: Field[Any], db_field: str, comment: str = ""
    ) -> str | None:
        """Non-PK `GENERATED ALWAYS AS (...)` column definition, or None if not applicable.

        Args:
            field_object: field to render.
            db_field: quoted column name source.
            comment: inline column comment SQL.
        Returns:
            The column definition string, or None when the field isn't a non-PK generated field
            or the dialect has no GENERATED_SQL for it.
        """
        if not (field_object.generated and not field_object.pk):
            return None
        generated_sql = field_object.get_generated_column_sql(self.client.dialect)
        if not generated_sql:
            return None
        return self._get_field_sql(
            db_field=db_field,
            field_type=f"{field_object.get_column_type(self.client.dialect)} {generated_sql}",
            nullable=field_object.null,
            unique=field_object.unique,
            is_pk=False,
            comment=comment,
        )

    def _get_fk_reference_string(
        self,
        constraint_name: str,
        db_field: str,
        table: str,
        field: str,
        on_delete: str,
        comment: str,
    ) -> str:
        return self.FK_TEMPLATE.format(
            db_field=db_field,
            table=table,
            field=self.quote(field),
            on_delete=on_delete,
            comment=comment,
        )

    def _backfill_default_sql_literal(self, field: Field[Any], default_value: Any, model: type[Model]) -> str:
        """Renders a field's Python ``default`` as the SQL literal existing rows are filled with.

        Raises:
            ConfigurationError: The default is a callable - computed once, it would give every row
                one value.
        """
        if callable(default_value):
            raise ConfigurationError(
                f"Can't backfill {model.__name__}.{field.model_field_name} during this schema "
                "change - its default is a callable, and computing it once here would give "
                "every existing row the same value instead of each row its own. Provide an "
                "explicit non-callable default for this migration, or backfill the column "
                "yourself in a RunPython step first."
            )
        return self.client.dialect.get_literal_sql(self.client.dialect.types.get_db_value(field, default_value, model))

    @staticmethod
    def _is_auto_now_field(field: Field[Any]) -> bool:
        """Whether ``field`` stamps the current time itself (``auto_now``/``auto_now_add``).

        Args:
            field: The field to check.

        Returns:
            True for an auto_now/auto_now_add DatetimeField or TimeField.
        """
        return isinstance(field, DatetimeField | TimeField) and (field.auto_now or field.auto_now_add)

    @classmethod
    def _needs_added_column_backfill(cls, field: Field[Any]) -> bool:
        """Whether adding ``field``'s column to a table that may already hold rows needs those
        rows filled first - a NOT NULL column whose only default lives in Python (the database
        has no value to give existing rows): a ``default``, or the current time of an
        ``auto_now``/``auto_now_add`` field."""
        return (
            not field.null
            and not field.pk
            and not field.generated
            and not field.has_db_default()
            and (field.default is not None or cls._is_auto_now_field(field))
        )

    def _auto_now_backfill_sql_literal(self, field: Field[Any], model: type[Model]) -> str:
        """The current time as a SQL literal for filling existing rows of an ``auto_now``/
        ``auto_now_add`` field, written exactly as a regular save() would write it.

        Args:
            field: An auto_now/auto_now_add DatetimeField or TimeField.
            model: The field's model.

        Returns:
            The SQL literal.
        """
        auto_now_field = cast("DatetimeField[Any] | TimeField[Any]", field)
        return self.client.dialect.get_literal_sql(
            self.client.dialect.types.get_db_value(auto_now_field, auto_now_field.get_auto_now_value(), model)
        )

    async def _added_column_backfill_sql_literal(self, field: Field[Any], model: type[Model]) -> str:
        """``field``'s Python default as a SQL literal for the rows that exist when its column is
        added. A callable (or async) default is evaluated once, so every existing row gets that
        same value.

        Args:
            field: The field being added.
            model: The field's model.

        Returns:
            The SQL literal.
        """
        if field.default is None and self._is_auto_now_field(field):
            return self._auto_now_backfill_sql_literal(field, model)
        default_value = field.default() if callable(field.default) else field.default
        if inspect.isawaitable(default_value):
            default_value = await default_value
        return self.client.dialect.get_literal_sql(self.client.dialect.types.get_db_value(field, default_value, model))

    async def _backfill_added_column(self, model: type[Model], field: Field[Any], db_field: str) -> None:
        """Fills the existing rows of a column just added as nullable with ``field``'s Python
        default, then makes the column NOT NULL.

        Args:
            model: The field's model.
            field: The field just added.
            db_field: The column's name.
        """
        backfill_value = await self._added_column_backfill_sql_literal(field, model)
        qualified_table = self._qualify_table_name(model._meta.db_table, model._meta.schema)
        await self._run_sql(
            f"UPDATE {qualified_table} SET {self.quote(db_field)} = {backfill_value} "  # nosec B608
            f"WHERE {self.quote(db_field)} IS NULL"
        )
        await self._set_added_column_not_null(model, db_field)

    async def _set_added_column_not_null(self, model: type[Model], db_field: str) -> None:
        """Makes a just-added, already backfilled column NOT NULL.

        Args:
            model: The column's model.
            db_field: The column's name.
        """
        qualified_table = self._qualify_table_name(model._meta.db_table, model._meta.schema)
        await self._run_sql(
            self.ALTER_FIELD_TEMPLATE.format(
                table=qualified_table, changes=self.ALTER_FIELD_NOT_NULL_TEMPLATE.format(column=self.quote(db_field))
            )
        )

    def _field_backfill_sql_literal(self, field: Field[Any], model: type[Model]) -> str | None:
        """The SQL literal existing rows get in a column: the field's ``default``, else its
        ``db_default``.

        Returns:
            The literal, None when the field has neither.
        """
        if field.default is not None:
            return self._backfill_default_sql_literal(field, field.default, model)
        if field.has_db_default():
            if hasattr(field.db_default, "get_sql"):
                return cast("str", field.db_default.get_sql(self.client.dialect))
            return self.client.dialect.get_literal_sql(
                self.client.dialect.types.get_db_value(field, field.db_default, model)
            )
        if self._is_auto_now_field(field):
            return self._auto_now_backfill_sql_literal(field, model)
        return None

    def _get_unique_constraint_sql(self, model: type[Model], field_names: list[str]) -> str:
        index_name = GeneratedNames.get_index_name(GeneratedNamePrefix.UNIQUE_CONSTRAINT, model, field_names)
        return self.UNIQUE_CONSTRAINT_CREATE_TEMPLATE.format(
            index_name=self.quote(index_name),
            nulls="",
            fields=", ".join([self.quote(f) for f in field_names]),
            include="",
        )

    def _get_unique_constraint_name(self, model: type[Model], field_names: list[str]) -> str:
        return GeneratedNames.get_index_name(GeneratedNamePrefix.UNIQUE_CONSTRAINT, model, field_names)

    def _get_composite_pk_constraint_sql(self, model: type[Model], field_names: list[str]) -> str:
        return self.PRIMARY_KEY_CONSTRAINT_CREATE_TEMPLATE.format(
            index_name=self.quote(GeneratedNames.get_index_name(GeneratedNamePrefix.PRIMARY_KEY, model, field_names)),
            fields=", ".join([self.quote(f) for f in field_names]),
        )

    @staticmethod
    def _get_exists_sql(safe: bool) -> str:
        """Returns the ``IF NOT EXISTS`` of a statement creating an object only when it's missing.

        Args:
            safe: Whether the object is created only when it doesn't exist yet.

        Returns:
            The clause with its trailing space, or an empty string.
        """
        return "IF NOT EXISTS " if safe else ""

    def _get_index_sql(
        self,
        model: type[Model],
        field_names: Sequence[str],
        safe: bool = False,
        index_name: str | None = None,
        index_type: str | None = None,
        extra: str | None = None,
        opclasses: Sequence[str] | None = None,
        unique: bool = False,
        orders: Sequence[str] | None = None,
        indexed_table_sql: str | None = None,
    ) -> str:
        # A unique index keeps its name on a database without unique constraints, as a plain index.
        enforces_uniqueness = unique and self.client.dialect.supports_unique_constraints
        template = self.UNIQUE_INDEX_CREATE_TEMPLATE if enforces_uniqueness else self.INDEX_CREATE_TEMPLATE
        prefix = GeneratedNamePrefix.UNIQUE_INDEX if unique else GeneratedNamePrefix.INDEX
        return template.format(
            exists=self._get_exists_sql(safe),
            index_name=self.quote(index_name or GeneratedNames.get_index_name(prefix, model, field_names)),
            table_name=indexed_table_sql or self._qualify_table_name(model._meta.db_table, model._meta.schema),
            fields=self._format_index_fields(
                field_names, opclasses, column_names=set(model._meta.fields_db_projection.values()), orders=orders
            ),
            index_type=self._format_index_type(index_type) if index_type else "",
            extra=f"{extra}" if extra else "",
        )

    def _format_index_type(self, index_type: str) -> str:
        """The index type as the dialect's CREATE INDEX statement writes it.

        Args:
            index_type: The index method (``Index.INDEX_TYPE``).

        Returns:
            The clause with its trailing space.
        """
        return f"{index_type} "

    def _get_unique_index_sql(
        self, table_name: str, field_names: Sequence[str], schema: str | None = None, safe: bool = False
    ) -> str:
        """Returns a ``CREATE UNIQUE INDEX`` over columns of a table without a model of its own
        (an automatic through table).

        Args:
            table_name: The table name.
            field_names: The indexed columns, in order.
            schema: The table's schema.
            safe: Whether the index is created only when it doesn't exist yet.

        Returns:
            The statement.
        """
        index_name = GeneratedNames.get_index_name(GeneratedNamePrefix.UNIQUE_INDEX, table_name, field_names)
        return self.UNIQUE_INDEX_CREATE_TEMPLATE.format(
            exists=self._get_exists_sql(safe),
            index_name=self.quote(index_name),
            index_type="",
            table_name=self._qualify_table_name(table_name, schema),
            fields=", ".join([self.quote(f) for f in field_names]),
            extra="",
        )

    def _get_table_index_sql(
        self, table_name: str, column_names: Sequence[str], schema: str | None = None, safe: bool = False
    ) -> str:
        """A plain ``CREATE INDEX`` on a table without a model of its own (an automatic through table).

        Args:
            table_name: The table name.
            column_names: The indexed columns, in order.
            schema: The table's schema.
            safe: Whether the index is created only when it doesn't exist yet.

        Returns:
            The statement.
        """
        return self.INDEX_CREATE_TEMPLATE.format(
            exists=self._get_exists_sql(safe),
            index_name=self.quote(GeneratedNames.get_index_name(GeneratedNamePrefix.INDEX, table_name, column_names)),
            index_type="",
            table_name=self._qualify_table_name(table_name, schema),
            fields=", ".join([self.quote(column_name) for column_name in column_names]),
            extra="",
        )

    def _get_inner_statements(self) -> list[str]:
        return []

    def _get_m2m_side_columns(
        self,
        through_table_name: str,
        side_keys: tuple[str, ...],
        target_meta: MetaInfo,
        on_delete: str,
        db_constraint: bool,
    ) -> tuple[list[str], ForeignKeyConstraint | None]:
        """The column definitions of one side of a many-to-many through table: one column with an
        inline ``REFERENCES`` for a single-column key, or a plain column per key part plus a
        table-level composite ``FOREIGN KEY`` constraint.
        """
        from hare.query.composite import KeyColumns

        pk_fields = target_meta.pk_fields if target_meta.has_composite_primary_key else (target_meta.pk,)
        pk_columns = KeyColumns.get_source_columns(target_meta)
        target_table = self._qualify_table_name(target_meta.db_table, target_meta.schema)
        # on_delete=SET_NULL needs nullable columns - on both sides, as the backward field has the
        # same on_delete.
        nullability = "" if on_delete == OnDelete.SET_NULL else " NOT NULL"

        if len(side_keys) == 1:
            column_type = self._get_pk_column_type(pk_fields[0])
            inline_fk = ""
            if db_constraint:
                inline_fk = self._get_fk_reference_string("", side_keys[0], target_table, pk_columns[0], on_delete, "")
            return [f"{self.quote(side_keys[0])} {column_type}{nullability}{inline_fk}"], None

        columns = [
            f"{self.quote(key)} {self._get_pk_column_type(pk_field)}{nullability}"
            for key, pk_field in zip(side_keys, pk_fields, strict=True)
        ]
        constraint = None
        if db_constraint:
            constraint = ForeignKeyConstraint(
                fields=side_keys,
                to_table=target_table,
                to_fields=pk_columns,
                on_delete=on_delete,
                name=GeneratedNames.get_foreign_key_name(
                    through_table_name, side_keys, target_meta.db_table, pk_columns
                ),
            )
        return columns, constraint

    def _get_pk_column_type(self, pk_field: Field[Any]) -> str:
        """Returns the column type of a primary key field - a one-to-one primary key takes the type
        of the key it points at.

        Args:
            pk_field: The primary key field.

        Returns:
            The SQL type.

        Raises:
            UnSupportedError: The field has no column type on this dialect.
        """
        if isinstance(pk_field, OneToOneFieldInstance):
            return self._get_pk_column_type(pk_field.related_model._meta.pk)
        if sql_type := pk_field.get_column_type(self.client.dialect):
            return sql_type
        raise UnSupportedError(f"Can't get SQL type of {pk_field} for {self.client.dialect.name}")

    def _get_m2m_table_definition(
        self, model: type[Model], field: ManyToManyFieldInstance[Model], safe: bool = False
    ) -> str | None:
        """Returns the statements creating an automatic through table: the table, its unique index
        over both sides for a ``unique`` field, and the indexes over its key columns.

        Args:
            model: The model declaring the M2M field.
            field: The M2M field.
            safe: Whether each object is created only when it doesn't exist yet.

        Returns:
            The statements, None for a field without an automatic through table (a ``through=``
            model has its own table, the generated backward field shares the forward one's).
        """
        if field._generated or field.through_model is not None:
            return None
        related_model = field.related_model
        if not related_model:
            return None
        m2m_schema = model._meta.schema
        through_table_name = field.through
        qualified_through = self._qualify_table_name(through_table_name, m2m_schema)

        # Without a database constraint (creates_foreign_key) the through-table's FK columns get
        # no FK constraint at all.
        backward_columns, backward_constraint = self._get_m2m_side_columns(
            through_table_name, field.backward_keys, model._meta, field.db_on_delete, self.creates_foreign_key(field)
        )
        forward_columns, forward_constraint = self._get_m2m_side_columns(
            through_table_name,
            field.forward_keys,
            related_model._meta,
            field.db_on_delete,
            self.creates_foreign_key(field),
        )
        column_lines = [*backward_columns, *forward_columns]
        for constraint in (backward_constraint, forward_constraint):
            if constraint:
                column_lines.append(self._get_foreign_key_constraint_clause(constraint))
        fields_string = "\n    {}\n".format(",\n    ".join(column_lines))
        m2m_create_string = self.M2M_TABLE_TEMPLATE.format(
            exists=self._get_exists_sql(safe),
            table_name=qualified_through,
            fields=fields_string,
            extra=self._table_generate_extra(table=through_table_name),
            comment=self._get_table_comment_sql(table=qualified_through, comment=field.description)
            if field.description
            else "",
        )
        m2m_create_string += self._post_table_hook()
        if field.unique and self.client.dialect.supports_unique_constraints:
            m2m_create_string += "\n" + self._get_unique_index_sql(
                through_table_name, [*field.backward_keys, *field.forward_keys], schema=m2m_schema, safe=safe
            )
        for index_keys in field.get_through_index_keys():
            m2m_create_string += "\n" + self._get_table_index_sql(
                through_table_name, index_keys, schema=m2m_schema, safe=safe
            )
        return m2m_create_string

    def _get_foreign_key_constraint_clause(self, constraint: ForeignKeyConstraint) -> str:
        """Returns a table-level ``CONSTRAINT ... FOREIGN KEY`` clause.

        Args:
            constraint: The foreign key constraint.

        Returns:
            The clause.
        """
        return self.FOREIGN_KEY_CONSTRAINT_CREATE_TEMPLATE.format(
            name=self.quote(constraint.name),
            fields=", ".join(self.quote(f) for f in constraint.fields),
            table=constraint.to_table,
            to_fields=", ".join(self.quote(f) for f in constraint.to_fields),
            on_delete=constraint.on_delete,
        )

    def _get_composite_fk_constraint(
        self, model: type[Model], fk_field: ForeignKeyFieldInstance[Model]
    ) -> ForeignKeyConstraint:
        """Table-level ForeignKeyConstraint for a composite-target FK/O2O - called once per
        logical field, not per shadow column."""
        to_fields = tuple(f.source_field or f.model_field_name for f in fk_field.to_field_instances)
        related_table = fk_field.related_model._meta.db_table
        return ForeignKeyConstraint(
            fields=fk_field.source_fields,
            to_table=self._qualify_table_name(related_table, fk_field.related_model._meta.schema),
            to_fields=to_fields,
            on_delete=fk_field.db_on_delete,
            name=GeneratedNames.get_foreign_key_name(
                model._meta.db_table, fk_field.source_fields, related_table, to_fields
            ),
        )

    def _get_fk_field_definition(self, model: type[Model], key_field_name: str, default: str = "") -> str:
        """Returns the column definition of a relation's key column.

        A single-column key with a database constraint carries an inline ``REFERENCES`` clause; a
        composite key's columns are plain, their table-level constraint added separately
        (``_get_composite_fk_constraint``); a relation without a database constraint
        (``creates_foreign_key``) gives a plain column.

        Args:
            model: The model declaring the relation.
            key_field_name: The key field's name.
            default: The column's ``DEFAULT`` clause, with its leading space, or an empty string.

        Returns:
            The column definition.
        """
        key_field = model._meta.fields_map[key_field_name]
        fk_field = cast("ForeignKeyFieldInstance[Model]", key_field.reference)
        db_field = model._meta.fields_db_projection[key_field_name]
        qualified_table = self._qualify_table_name(model._meta.db_table, model._meta.schema)
        comment = (
            self._get_column_comment_sql(table=qualified_table, column=db_field, comment=fk_field.description)
            if fk_field.description
            else ""
        )
        column_type = key_field.get_column_type(self.client.dialect)
        if len(fk_field.to_field_instances) > 1 or not self.creates_foreign_key(fk_field):
            return self._get_field_sql(
                db_field=db_field,
                field_type=column_type,
                nullable=key_field.null,
                unique=key_field.unique and len(fk_field.to_field_instances) == 1,
                is_pk=key_field.pk,
                comment=comment,
                default=default,
            )

        to_field_name = fk_field.to_field_instance.source_field or fk_field.to_field_instance.model_field_name
        related_model = fk_field.related_model
        return self._get_field_sql(
            db_field=db_field,
            field_type=column_type,
            nullable=key_field.null,
            unique=key_field.unique,
            is_pk=key_field.pk,
            comment="",
            default=default,
        ) + self._get_fk_reference_string(
            constraint_name=GeneratedNames.get_foreign_key_name(
                model._meta.db_table, (db_field,), related_model._meta.db_table, (to_field_name,)
            ),
            db_field=db_field,
            table=self._qualify_table_name(related_model._meta.db_table, related_model._meta.schema),
            field=to_field_name,
            on_delete=fk_field.db_on_delete,
            comment=comment,
        )

    def _get_column_default_sql(self, field_object: Field[Any], model: type[Model]) -> str:
        """Returns a column's ``DEFAULT`` clause from its field's ``db_default``.

        Args:
            field_object: The field.
            model: The field's model.

        Returns:
            The clause with its leading space, or an empty string for a field without ``db_default``.
        """
        if not field_object.has_db_default():
            return ""
        db_default = field_object.db_default
        if hasattr(db_default, "get_sql"):
            return f" DEFAULT {db_default.get_sql(self.client.dialect)}"
        db_value = self.client.dialect.types.get_db_value(field_object, db_default, model)
        return f" DEFAULT {self.client.dialect.get_literal_sql(db_value)}"

    def _get_column_definitions(
        self, model: type[Model]
    ) -> tuple[list[str], set[tuple[str | None, str]], list[ForeignKeyConstraint]]:
        """Returns the column definitions of a model's table.

        Args:
            model: The model.

        Returns:
            The column definitions in field order; the ``(schema, table)`` of every table a real
            foreign key constraint points at; and the table-level constraints of composite keys.
        """
        column_definitions: list[str] = []
        references: set[tuple[str | None, str]] = set()
        composite_fk_constraints: list[ForeignKeyConstraint] = []
        qualified_table_name = self._qualify_table_name(model._meta.db_table, model._meta.schema)
        for field_name, db_field in model._meta.fields_db_projection.items():
            field_object = model._meta.fields_map[field_name]
            comment = (
                self._get_column_comment_sql(
                    table=qualified_table_name, column=db_field, comment=field_object.description
                )
                if field_object.description
                else ""
            )
            if field_object.pk and field_object.generated:
                generated_sql = field_object.get_generated_sql(self.client.dialect)
                if generated_sql:
                    column_definitions.append(
                        self.GENERATED_PK_TEMPLATE.format(
                            field_name=self.quote(db_field), generated_sql=generated_sql, comment=comment
                        )
                    )
                    continue
            if field_object.generated and not field_object.pk:
                generated_column_sql = self._get_non_pk_generated_field_sql(field_object, db_field, comment)
                if generated_column_sql:
                    column_definitions.append(generated_column_sql)
                    continue
            default = self._get_column_default_sql(field_object, model)
            reference = cast("ForeignKeyFieldInstance[Model] | None", getattr(field_object, "reference", None))
            if reference:
                column_definitions.append(self._get_fk_field_definition(model, field_name, default))
                if self.creates_foreign_key(reference):
                    # Only a real constraint orders the tables: a relation without one has
                    # no DDL-level dependency at all, and must never make a model-level cycle look
                    # like a cyclic reference between tables.
                    related_meta = reference.related_model._meta
                    references.add((related_meta.schema, related_meta.db_table))
                    if len(reference.to_field_instances) > 1 and db_field == reference.source_fields[0]:
                        composite_fk_constraints.append(self._get_composite_fk_constraint(model, reference))
                continue
            column_definitions.append(
                self._get_field_sql(
                    db_field=db_field,
                    field_type=field_object.get_column_type(self.client.dialect),
                    nullable=field_object.null,
                    unique=field_object.unique,
                    is_pk=field_object.pk,
                    comment=comment,
                    default=default,
                )
            )
        return column_definitions, references, composite_fk_constraints

    def _get_model_index_sqls(self, model: type[Model], safe: bool = False) -> list[str]:
        """Returns the statements creating a model's indexes - ``index=True`` fields and
        ``Meta.indexes`` - each once.

        Args:
            model: The model.
            safe: Whether each index is created only when it doesn't exist yet.

        Returns:
            The statements.
        """
        index_sqls = [
            self._get_index_sql(model, list(column_names), safe=safe)
            for _field_name, column_names in model._meta.get_field_index_columns()
        ]
        for index in model._meta.indexes:
            if isinstance(index, Index):
                index_sqls.append(index.get_sql(self, model, safe))
            else:
                index_sqls.append(self._get_index_sql(model, model._meta.get_column_names(index), safe=safe))
        return [index_sql for index_sql in dict.fromkeys(index_sqls) if index_sql]

    def _get_create_constraint_sqls(self, model: type[Model], safe: bool = False) -> tuple[list[str], list[str]]:
        """Returns the DDL of a new table's ``Meta.constraints``. A constraint goes into the ``CREATE
        TABLE`` body. A unique constraint is instead a unique index created after the table - only
        when missing, with ``safe`` - where the database can't drop a table constraint in place, and
        wherever it has a condition. A database without unique constraints gets none.

        Args:
            model: The model.
            safe: Whether each object is created only when it doesn't exist yet.

        Returns:
            The clauses inside ``CREATE TABLE`` and the statements after it.

        Raises:
            ConfigurationError: An entry of ``Meta.constraints`` isn't a constraint.
            UnSupportedError: A constraint the dialect has no DDL for.
        """
        dialect = self.client.dialect
        qualified_table_name = self._qualify_table_name(model._meta.db_table, model._meta.schema)
        table_clauses: list[str] = []
        statements: list[str] = []
        for constraint in model._meta.constraints:
            if isinstance(constraint, CheckConstraint):
                constraint_sql = self.CHECK_CONSTRAINT_CREATE_TEMPLATE.format(
                    name=self.quote(constraint.name),
                    check=ConstraintCondition.get_sql(constraint.check, model, self.client),
                )
            elif isinstance(constraint, ExclusionConstraint):
                constraint_sql = self._exclusion_constraint_sql(model, constraint)
            elif isinstance(constraint, UniqueConstraint):
                if not dialect.supports_unique_constraints:
                    continue
                self._check_unique_constraint_supported(constraint)
                constraint_column_names = model._meta.get_column_names(constraint.fields)
                index_name = constraint.name or GeneratedNames.get_index_name(
                    GeneratedNamePrefix.UNIQUE_CONSTRAINT, model, constraint_column_names
                )
                quoted_columns = ", ".join(self.quote(column_name) for column_name in constraint_column_names)
                include_sql = constraint.get_include_sql(model, dialect, self.quote)
                if constraint.condition or not dialect.supports_adding_constraints:
                    where_sql = (
                        f" WHERE ({ConstraintCondition.get_sql(constraint.condition, model, self.client)})"
                        if constraint.condition
                        else ""
                    )
                    statements.append(
                        f"CREATE UNIQUE INDEX {self._get_exists_sql(safe)}{self.quote(index_name)} "
                        f"ON {qualified_table_name} "
                        f"({quoted_columns}){include_sql}{constraint.get_nulls_sql(dialect)}{where_sql}"
                    )
                    continue
                constraint_sql = self.UNIQUE_CONSTRAINT_CREATE_TEMPLATE.format(
                    index_name=self.quote(index_name),
                    nulls=constraint.get_nulls_sql(dialect),
                    fields=quoted_columns,
                    include=include_sql,
                )
                if constraint.deferrable:
                    constraint_sql += " DEFERRABLE INITIALLY " + (
                        "DEFERRED" if constraint.initially_deferred else "IMMEDIATE"
                    )
            else:  # pragma: nocoverage
                raise ConfigurationError(f"Unsupported Meta.constraints entry: {constraint!r}")
            table_clauses.append(constraint_sql)
        return table_clauses, statements

    def _get_model_sql_data(
        self,
        model: type[Model],
        safe: bool = False,
        model_table_keys: frozenset[tuple[str | None, str]] = frozenset(),
    ) -> ModelSqlData:
        """Returns the DDL creating a model: its table with its indexes, constraints and comments,
        and its automatic through tables.

        Args:
            model: The model.
            safe: Whether each object is created only when it doesn't exist yet.
            model_table_keys: The ``(schema, table)`` of the other models created with it - an
                automatic through table named like one of them is that model's table, not a
                through table to create.

        Returns:
            The statements and the tables the model's table references.
        """
        schema = model._meta.schema
        qualified_table_name = self._qualify_table_name(model._meta.db_table, schema)
        table_definitions, references, composite_fk_constraints = self._get_column_definitions(model)

        if model._meta.has_composite_primary_key:
            # A composite primary key's fields are plain columns - the table-level PRIMARY KEY
            # names all of them.
            composite_pk_columns = [model._meta.fields_db_projection[name] for name in model._meta.pk_attr]
            table_definitions.append(self._get_composite_pk_constraint_sql(model, composite_pk_columns))
        # Inline, as every database can create a table-level FOREIGN KEY with the table.
        table_definitions.extend(
            self._get_foreign_key_constraint_clause(constraint) for constraint in composite_fk_constraints
        )
        constraint_clauses, constraint_statements = self._get_create_constraint_sqls(model, safe)
        table_definitions.extend(constraint_clauses)
        table_definitions.extend(self._get_inner_statements())

        table_options = model._meta.get_table_options(self.client.dialect)
        if table_options is not None:
            table_options.raise_if_unsupported(model)
        table_create_string = self.TABLE_CREATE_TEMPLATE.format(
            prefix=table_options.get_create_prefix_sql() if table_options else "",
            exists=self._get_exists_sql(safe),
            table_name=qualified_table_name,
            fields="\n    {}\n".format(",\n    ".join(table_definitions)),
            comment=self._get_table_comment_sql(table=qualified_table_name, comment=model._meta.table_description)
            if model._meta.table_description
            else "",
            extra=self._table_generate_extra(table=model._meta.db_table)
            + (table_options.get_create_suffix_sql(model, self.quote) if table_options else ""),
        )
        table_create_string = "\n".join(
            [
                table_create_string,
                *self._get_partition_create_sqls(model, safe),
                *self._get_model_index_sqls(model, safe),
            ]
        )
        table_create_string += self._post_table_hook()

        m2m_tables_sql = []
        for m2m_field_name in sorted(model._meta.m2m_fields):
            m2m_field = cast("ManyToManyFieldInstance[Model]", model._meta.fields_map[m2m_field_name])
            if (m2m_field.through_schema or schema, m2m_field.through) in model_table_keys:
                continue
            if m2m_create_string := self._get_m2m_table_definition(model, m2m_field, safe):
                m2m_tables_sql.append(m2m_create_string)

        return ModelSqlData(
            table_key=(schema, model._meta.db_table),
            model=model,
            table_sql=table_create_string,
            constraint_sqls=constraint_statements,
            references=references,
            m2m_tables_sql=m2m_tables_sql,
        )

    def _get_models_to_create(self) -> list[type[Model]]:
        """Returns every registered model this connection creates the table of - not a
        ``Meta.managed = False`` one, whose table is never hare's, and not a swapped one, which has
        none.

        Returns:
            The models.
        """
        from hare import Hare
        from hare.core.context import HareContext

        context = HareContext.get_current()
        apps = context.apps if context is not None and context._inited and context.apps is not None else Hare.apps
        if not apps:
            return []
        models_to_create = []
        for model in apps.get_models_iterable():
            if model._meta.db != self.client:
                continue
            model._check()
            if model._meta.managed is not False and model._meta.swapped is None:
                models_to_create.append(model)
        return models_to_create

    def _get_schema_create_sql(self, schema: str, safe: bool) -> str:
        """Returns the statement creating a database schema, or an empty string where the
        database has no schemas.

        Args:
            schema: The schema's name.
            safe: Whether it is created only when it doesn't exist yet.

        Returns:
            The statement.
        """
        if not self.client.dialect.supports_schemas:
            return ""
        return f"CREATE SCHEMA {self._get_exists_sql(safe)}{self.quote(schema)};"

    def _get_extension_create_sql(self, extension: str) -> str:
        """Returns the statement installing a database extension unless it is installed, or an
        empty string where the database has no extensions.

        Args:
            extension: The extension's name.

        Returns:
            The statement.
        """
        return ""

    def _get_required_extensions(self, models: Sequence[type[Model]]) -> list[str]:
        """Returns every extension the models need - ``Meta.extensions`` entries, the extension of
        a field type an extension provides (``CitextField``), and the operator class extension of
        an ``ExclusionConstraint`` - each once, in first-use order.

        Args:
            models: The models.

        Returns:
            The extension names.
        """
        extensions: dict[str, None] = {}
        for model in models:
            for extension in model._meta.extensions:
                extensions[extension] = None
            for field in model._meta.fields_map.values():
                if field.requires_extension:
                    extensions[field.requires_extension] = None
            for constraint in model._meta.constraints:
                if isinstance(constraint, ExclusionConstraint) and (
                    constraint_extension := self.client.dialect.get_exclusion_constraint_extension(
                        constraint, model._meta.fields_map
                    )
                ):
                    extensions[constraint_extension] = None
        return list(extensions)

    def get_create_schema_sql(self, safe: bool = True) -> str:
        """Returns the DDL creating every model of this connection, in the order the tables
        reference each other: database schemas, extensions, tables with their indexes and
        constraints, automatic through tables, then triggers - a trigger body may use any table.

        Args:
            safe: Whether each object is created only when it doesn't exist yet.

        Returns:
            The DDL script.

        Raises:
            ConfigurationError: The tables' foreign keys reference each other in a cycle.
        """
        models_to_create = self._get_models_to_create()
        schemas = dict.fromkeys(model._meta.schema for model in models_to_create if model._meta.schema)
        statements = [sql for schema in schemas if (sql := self._get_schema_create_sql(schema, safe))]
        statements += [
            sql
            for extension in self._get_required_extensions(models_to_create)
            if (sql := self._get_extension_create_sql(extension))
        ]

        model_table_keys = frozenset((model._meta.schema, model._meta.db_table) for model in models_to_create)
        pending_tables = [self._get_model_sql_data(model, safe, model_table_keys) for model in models_to_create]
        created_table_keys = {sql_data.table_key for sql_data in pending_tables}
        m2m_tables_sql: list[str] = []
        created: set[tuple[str | None, str]] = set()
        while pending_tables:
            # A reference to a table hare doesn't create (a Meta.managed = False model's) never
            # holds up the order.
            next_table = next(
                (
                    sql_data
                    for sql_data in pending_tables
                    if (sql_data.references & created_table_keys) <= created | {sql_data.table_key}
                ),
                None,
            )
            if next_table is None:
                raise ConfigurationError("Can't create schema due to cyclic fk references")
            pending_tables.remove(next_table)
            created.add(next_table.table_key)
            statements.append(next_table.get_table_creation_sql())
            m2m_tables_sql += next_table.m2m_tables_sql
        statements += m2m_tables_sql
        statements += [
            statement
            for model in models_to_create
            for trigger in model._meta.triggers
            for statement in self.get_trigger_create_sqls(model, trigger, safe=safe)
        ]
        return "\n".join(statements)

    async def alter_table_options(
        self, model: type[Model], old_options: TableOptions | None, new_options: TableOptions | None
    ) -> None:
        """Applies a change of the table's ``Meta.table_options`` for this dialect - by rebuilding
        the table with the new options, which a dialect whose ALTER TABLE can change them in place
        overrides.

        Args:
            model: The model rendered with its new options.
            old_options: The dialect's previous options, None for none.
            new_options: The dialect's new options, None for none.
        """
        await self._remake_table(model)

    def _get_partition_create_sqls(self, model: type[Model], safe: bool) -> list[str]:
        """The statements creating the partitions of a model's table, run right after its
        ``CREATE TABLE`` - none on a dialect without partitioned tables.

        Args:
            model: The model.
            safe: Whether each partition is created only when it doesn't exist yet.

        Returns:
            The statements.
        """
        return []

    async def add_partition(self, model: type[Model], partition: Any) -> None:
        """Adds a partition to a model's table (``TableOptions.get_partitions()``).

        Args:
            model: The model rendered with the partition.
            partition: The partition.

        Raises:
            UnSupportedError: The dialect has no partitions to add one at a time.
        """
        raise UnSupportedError(f"The {self.client.dialect.name} dialect can't add a partition to a table")

    async def remove_partition(self, model: type[Model], partition: Any) -> None:
        """Removes a partition of a model's table with its rows.

        Args:
            model: The model.
            partition: The partition.

        Raises:
            UnSupportedError: The dialect has no partitions to remove one at a time.
        """
        raise UnSupportedError(f"The {self.client.dialect.name} dialect can't remove a partition of a table")

    async def create_model(self, model: type[Model]) -> None:
        """Creates a model's table with its indexes, triggers and constraints, and its automatic
        through tables.

        Args:
            model: The model.
        """
        model_sql_data = self._get_model_sql_data(model)
        await self._run_sql("\n".join([model_sql_data.table_sql, *model_sql_data.m2m_tables_sql]))
        for trigger in model._meta.triggers:
            await self.add_trigger(model, trigger)
        for constraint_sql in model_sql_data.constraint_sqls:
            await self._run_sql(constraint_sql)

    async def rename_table(self, model: type[Model], old_name: str, new_name: str) -> None:
        if old_name == new_name:
            return
        schema = model._meta.schema
        await self._run_sql(
            self.RENAME_TABLE_TEMPLATE.format(
                old_table=self._qualify_table_name(old_name, schema),
                new_table=self.quote(new_name),
            )
        )

    async def delete_model(self, model: type[Model]) -> None:
        schema = model._meta.schema
        for field_name in sorted(model._meta.m2m_fields):
            field = cast("ManyToManyFieldInstance[Model]", model._meta.fields_map[field_name])
            if field.through_model is not None:
                # A ManyToManyField(through=SomeModel)'s table belongs to SomeModel, which gets
                # its own separate DropModel operation when it's actually meant to go away -
                # deleting THIS model must not also drop a table another model still owns.
                continue
            await self._run_sql(
                self.DELETE_TABLE_TEMPLATE.format(table=self._qualify_table_name(field.through, schema))
            )

        await self._run_sql(
            self.DELETE_TABLE_TEMPLATE.format(table=self._qualify_table_name(model._meta.db_table, schema))
        )

    def creates_foreign_key(self, relation_field: Field[Any]) -> bool:
        """Whether a relation's table gets a FOREIGN KEY constraint.

        Args:
            relation_field: A FK/O2O or many-to-many field.

        Returns:
            True for a ``db_constraint=True`` relation on a database that enforces foreign keys
            (``Dialect.supports_foreign_keys``).
        """
        return bool(getattr(relation_field, "db_constraint", False)) and self.client.dialect.supports_foreign_keys

    def _foreign_key_changed(self, old_field: Field[Any], new_field: Field[Any]) -> bool:
        """Whether a relation's FOREIGN KEY constraint differs between two versions of the field -
        its ``on_delete`` action, or whether there is one at all.

        Args:
            old_field: The relation field before the change.
            new_field: The relation field after it.

        Returns:
            False on a database without foreign keys, which has no constraint to change.
        """
        if not self.client.dialect.supports_foreign_keys:
            return False
        return getattr(old_field, "on_delete", None) != getattr(new_field, "on_delete", None) or (
            self.creates_foreign_key(old_field) != self.creates_foreign_key(new_field)
        )

    async def _add_composite_fk_field(self, model: type[Model], field: ForeignKeyFieldInstance[Model]) -> None:
        """Adds a relation to a composite key to an existing table: one ``ADD COLUMN`` per key column,
        then one table-level ``FOREIGN KEY`` constraint.
        """
        for key_field_name in field.source_fields:
            db_field = model._meta.fields_db_projection[key_field_name]
            key_field = model._meta.fields_map[key_field_name]
            field_definition = self._get_field_sql(
                db_field=db_field,
                field_type=key_field.get_column_type(self.client.dialect),
                nullable=key_field.null,
                unique=False,
                is_pk=False,
                comment="",
            )
            await self._run_sql(
                self.ADD_FIELD_TEMPLATE.format(
                    table=self._qualify_table_name(model._meta.db_table, model._meta.schema),
                    definition=field_definition,
                )
            )
        if self.creates_foreign_key(field):
            await self.add_constraint(model, self._get_composite_fk_constraint(model, field))

    async def add_field(self, model: type[Model], field_name: str) -> None:
        field = model._meta.fields_map[field_name]
        if isinstance(field, ManyToManyFieldInstance):
            table_string = self._get_m2m_table_definition(model, field)
            if table_string:
                await self._run_sql(table_string)
            return

        needs_backfill = False
        if isinstance(field, ForeignKeyFieldInstance):
            if len(field.source_fields) > 1:
                await self._add_composite_fk_field(model, field)
                return
            key_field_name = field.source_field or field_name
            field_definition = self._get_fk_field_definition(model, key_field_name)
        else:
            db_field = model._meta.fields_db_projection[field_name]
            # Added nullable first, filled, then made NOT NULL - a NOT NULL column with only a
            # Python default can't be added to a table that already holds rows in one step.
            needs_backfill = self._needs_added_column_backfill(field)
            comment = (
                self._get_column_comment_sql(
                    table=self._qualify_table_name(model._meta.db_table, model._meta.schema),
                    column=db_field,
                    comment=field.description,
                )
                if field.description
                else ""
            )

            if field.generated and not field.pk:
                generated_sql = field.get_generated_column_sql(self.client.dialect)
            else:
                generated_sql = None

            field_type = field.get_column_type(self.client.dialect)
            if generated_sql:
                field_type = f"{field_type} {generated_sql}"

            field_definition = self._get_field_sql(
                db_field=db_field,
                field_type=field_type,
                nullable=field.null or needs_backfill,
                unique=field.unique,
                is_pk=field.pk,
                comment=comment,
            )

        if field.has_db_default():
            if hasattr(field.db_default, "get_sql"):
                field_definition += f" DEFAULT {field.db_default.get_sql(self.client.dialect)}"
            else:
                db_val = self.client.dialect.types.get_db_value(field, field.db_default, model)
                escaped = self.client.dialect.get_literal_sql(db_val)
                field_definition += f" DEFAULT {escaped}"

        await self._run_sql(
            self.ADD_FIELD_TEMPLATE.format(
                table=self._qualify_table_name(model._meta.db_table, model._meta.schema),
                definition=field_definition,
            )
        )
        if needs_backfill:
            await self._backfill_added_column(model, field, db_field)

    async def _alter_m2m_field(
        self,
        model: type[Model],
        old_field: ManyToManyFieldInstance[Model],
        new_field: ManyToManyFieldInstance[Model],
    ) -> None:
        if old_field.through_model is not None and new_field.through_model is not None:
            # A ManyToManyField(through=SomeModel)'s table/columns are SomeModel's own - any
            # rename/alteration of them goes through SomeModel's own AlterField/RenameField
            # operations, not this M2M-specific through-table path.
            return
        if old_field.through_model is not None or new_field.through_model is not None:
            await self._move_m2m_rows_between_through_tables(model, old_field, new_field)
            return
        schema = model._meta.schema
        if old_field.through != new_field.through:
            await self._run_sql(
                self.RENAME_TABLE_TEMPLATE.format(
                    old_table=self._qualify_table_name(old_field.through, schema),
                    new_table=self.quote(new_field.through),
                )
            )

        qualified_through = self._qualify_table_name(new_field.through, schema)
        for old_key, new_key in zip(old_field.forward_keys, new_field.forward_keys, strict=True):
            if old_key != new_key:
                await self._run_sql(
                    self.RENAME_FIELD_TEMPLATE.format(
                        table=qualified_through, old_column=self.quote(old_key), new_column=self.quote(new_key)
                    )
                )

        for old_key, new_key in zip(old_field.backward_keys, new_field.backward_keys, strict=True):
            if old_key != new_key:
                await self._run_sql(
                    self.RENAME_FIELD_TEMPLATE.format(
                        table=qualified_through, old_column=self.quote(old_key), new_column=self.quote(new_key)
                    )
                )

        # Before the rebuild below: SQLite's rebuild recreates the table with the new definition's
        # indexes, which a later diff against the old ones would drop and create a second time.
        await self._alter_m2m_through_indexes(schema, old_field, new_field)
        if self._foreign_key_changed(old_field, new_field):
            await self._rebuild_m2m_through_foreign_keys(model, new_field)

    async def _alter_m2m_through_indexes(
        self,
        schema: str | None,
        old_field: ManyToManyFieldInstance[Model],
        new_field: ManyToManyFieldInstance[Model],
    ) -> None:
        """Drops the automatic through table's plain key indexes the old definition had and the
        new one doesn't, and creates the new ones - an index is named after its table and
        columns, so a renamed table or key column moves it too.

        Args:
            schema: The through table's schema.
            old_field: The relation's previous definition.
            new_field: The relation's new definition.
        """
        old_index_names = {
            GeneratedNames.get_index_name(GeneratedNamePrefix.INDEX, old_field.through, index_keys)
            for index_keys in old_field.get_through_index_keys()
        }
        new_index_keys_by_name = {
            GeneratedNames.get_index_name(GeneratedNamePrefix.INDEX, new_field.through, index_keys): index_keys
            for index_keys in new_field.get_through_index_keys()
        }
        for index_name in sorted(old_index_names - new_index_keys_by_name.keys()):
            await self._run_sql(self.DROP_INDEX_TEMPLATE.format(name=self._qualify_table_name(index_name, schema)))
        for index_name, index_keys in new_index_keys_by_name.items():
            if index_name not in old_index_names:
                await self._run_sql(self._get_table_index_sql(new_field.through, index_keys, schema=schema))

    async def _move_m2m_rows_between_through_tables(
        self,
        model: type[Model],
        old_field: ManyToManyFieldInstance[Model],
        new_field: ManyToManyFieldInstance[Model],
    ) -> None:
        """Carries an M2M relation's rows over between hare's automatic through table and a
        through model's table (either direction).

        The through model's own table is created/dropped by its CreateModel/DeleteModel - only the
        automatic table is created or dropped here.

        Args:
            model: The model owning the M2M field, rendered from the new state.
            old_field: The relation's current definition.
            new_field: The relation's new definition.
        """
        if new_field.through_model is None:
            table_definition = self._get_m2m_table_definition(model, new_field)
            if table_definition:
                await self._run_sql(table_definition)
        old_table = self._qualify_table_name(old_field.through, old_field.through_schema or model._meta.schema)
        new_table = self._qualify_table_name(new_field.through, new_field.through_schema or model._meta.schema)
        old_columns = ", ".join(self.quote(key) for key in (*old_field.backward_keys, *old_field.forward_keys))
        new_column_names = [*new_field.backward_keys, *new_field.forward_keys]
        default_values: list[str] = []
        if new_field.through_model is not None:
            for column_name, default_value in (await self._get_through_model_default_values(new_field)).items():
                if column_name not in new_column_names:
                    new_column_names.append(column_name)
                    default_values.append(default_value)
        new_columns = ", ".join(self.quote(column_name) for column_name in new_column_names)
        selected_values = ", ".join([old_columns, *default_values])
        # The automatic table holds each pair once - a through model may repeat one.
        distinct = "DISTINCT " if new_field.through_model is None else ""
        await self._run_sql(
            f"INSERT INTO {new_table} ({new_columns}) SELECT {distinct}{selected_values} FROM {old_table}"  # nosec B608
        )
        if old_field.through_model is None:
            await self._run_sql(self.DELETE_TABLE_TEMPLATE.format(table=old_table))

    async def _get_through_model_default_values(self, m2m_field: ManyToManyFieldInstance[Model]) -> dict[str, str]:
        """The Python defaults of a through model's own columns, for the rows carried over into
        its table. A callable default is evaluated once - every carried-over row gets that value.

        Args:
            m2m_field: An M2M field declared with ``through=SomeModel``, relations initialized.

        Returns:
            Column name -> the default as a SQL literal, for each column with a Python default
            (or the current time of an auto_now/auto_now_add field) and no database default.

        Raises:
            ConfigurationError: A unique column's default is callable - one value computed for
                every row can't be unique.
        """
        through_model = m2m_field.through_model_class
        if through_model is None:
            return {}
        unique_constraint_field_names = {
            field_name
            for constraint in through_model._meta.constraints
            if isinstance(constraint, UniqueConstraint)
            for field_name in constraint.fields
        }
        default_values: dict[str, str] = {}
        for field_name, column_name in through_model._meta.fields_db_projection.items():
            field = through_model._meta.fields_map[field_name]
            if field.pk or field.generated or field.has_db_default():
                continue
            if field.default is None and not self._is_auto_now_field(field):
                continue
            if callable(field.default) and (field.unique or field_name in unique_constraint_field_names):
                raise ConfigurationError(
                    f"Can't carry the rows of {m2m_field.model_field_name!r} over into "
                    f"{through_model.__name__}'s table - its unique field {field_name!r} has a callable "
                    "default, and computing it once would give every row the same value. Give it a "
                    "db_default, or fill the through table yourself in a RunPython step."
                )
            default_values[column_name] = await self._added_column_backfill_sql_literal(field, through_model)
        return default_values

    async def _alter_generated_field(self, model: type[Model], old_field: Field[Any], new_field: Field[Any]) -> bool:
        if old_field.pk or new_field.pk:
            return False

        old_generated_sql = old_field.get_generated_sql(self.client.dialect) if old_field.generated else None
        new_generated_sql = new_field.get_generated_sql(self.client.dialect) if new_field.generated else None
        # The generated SQL doesn't show output_field's type - the column type is compared too, so a
        # change of output_field alone is seen.
        old_sql_type = old_field.get_column_type(self.client.dialect) if old_field.generated else None
        new_sql_type = new_field.get_column_type(self.client.dialect) if new_field.generated else None
        if old_generated_sql == new_generated_sql and old_sql_type == new_sql_type:
            return False
        if old_field.generated or new_field.generated:
            raise ConfigurationError(
                f"Modifying generated fields is not supported - the field {new_field} "
                "must be removed and re-added with the new definition."
            )
        return False

    def _get_generated_fields_depending_on_column(self, model: type[Model], db_field: str) -> list[Field[Any]]:
        """Non-pk GeneratedField entries on `model` whose own expression references `db_field`.

        Args:
            model: The model to scan.
            db_field: The database column name to look for.

        Returns:
            The dependent GeneratedField instances.
        """
        dependent_fields: list[Field[Any]] = []
        for field in model._meta.fields_map.values():
            if not isinstance(field, GeneratedField) or field.pk:
                continue
            expressions = field.expression.values() if isinstance(field.expression, dict) else [field.expression]
            if any(re.search(rf"\b{re.escape(db_field)}\b", expression) for expression in expressions):
                dependent_fields.append(field)
        return dependent_fields

    async def _check_alter_field_narrowing_data_loss(
        self, qualified_table: str, old_field: Field[Any], new_field: Field[Any], quoted_column: str
    ) -> None:
        """Raise if an existing row doesn't fit ``new_field`` - a value the type change would
        silently truncate or round, or one the narrower column would hold beyond its declared size.

        Args:
            qualified_table: The already schema-qualified, quoted table name.
            old_field: The field's previous definition.
            new_field: The field's new, narrower definition.
            quoted_column: The already-quoted column name to check.

        Raises:
            FieldNarrowingDataLossError: At least one existing row would overflow the new type.
        """
        limit = new_field.get_narrowing_limit(old_field)
        if limit is None:
            return
        overflow_predicate = self.get_narrowing_overflow_predicate_sql(limit, quoted_column)
        rows = await self.client.execute_dicts(
            f"SELECT count(*) AS overflow_count FROM {qualified_table} WHERE {overflow_predicate}"  # nosec B608
        )
        overflow_count = rows[0]["overflow_count"]
        if overflow_count:
            raise FieldNarrowingDataLossError(
                f"Cannot narrow column {quoted_column} on {qualified_table} to the new definition "
                f"of field '{new_field.model_field_name}' - {overflow_count} existing row(s) would "
                "not fit the new column type. Clean up or widen the offending "
                "values by hand first, then retry the migration."
            )

    def get_backfill_batch_sql(
        self, qualified_table: str, quoted_column: str, quoted_pk_column: str, batch_size: int
    ) -> str:
        """One batch of filling a column's NULLs: sets the column to parameter 1 in up to
        ``batch_size`` rows where it is NULL and differs, NULL-safely, from parameter 2 - the same
        value. Without the second condition a NULL fill value would never end the batches.

        Args:
            qualified_table: The schema-qualified, quoted table.
            quoted_column: The quoted column to fill.
            quoted_pk_column: The quoted primary key column a batch's rows are picked by.
            batch_size: The most rows a batch writes.

        Returns:
            The statement.
        """
        dialect = self.client.dialect
        return (
            f"UPDATE {qualified_table} SET {quoted_column} = {dialect.get_placeholder(1)} "  # nosec B608
            f"WHERE {quoted_pk_column} IN ("
            f"SELECT {quoted_pk_column} FROM {qualified_table} "
            f"WHERE {quoted_column} IS NULL "
            f"AND {quoted_column} {dialect.is_distinct_from_operator} {dialect.get_placeholder(2)} "
            f"LIMIT {batch_size})"
        )

    def get_null_count_sql(self, qualified_table: str, quoted_column: str) -> str:
        """Counts a column's NULLs, as ``null_count``.

        Args:
            qualified_table: The schema-qualified, quoted table.
            quoted_column: The quoted column.

        Returns:
            The statement.
        """
        return f"SELECT COUNT(*) AS null_count FROM {qualified_table} WHERE {quoted_column} IS NULL"  # nosec B608

    def get_narrowing_overflow_predicate_sql(self, limit: NarrowingLimit, quoted_column: str) -> str:
        """A WHERE predicate matching the rows whose value is beyond a narrowing limit.

        Args:
            limit: The limit.
            quoted_column: The quoted column.

        Returns:
            The predicate.
        """
        if isinstance(limit, TextLengthLimit):
            if limit.source == NarrowedValueSource.TEXT:
                text_sql = quoted_column
            elif limit.source == NarrowedValueSource.BOOLEAN:
                text_sql = f"CASE WHEN {quoted_column} THEN 'true' ELSE 'false' END"
            else:
                text_sql = f"CAST({quoted_column} AS TEXT)"
            return f"length({text_sql}) > {limit.max_length}"
        if isinstance(limit, DecimalDigitsLimit):
            return self.get_decimal_overflow_predicate_sql(quoted_column, limit.max_digits, limit.decimal_places)
        if not limit.checks_fraction:
            return f"{quoted_column} < {limit.lowest} OR {quoted_column} > {limit.highest}"
        whole_digits = len(str(max(abs(limit.lowest), abs(limit.highest))))
        number_sql = f"CAST({quoted_column} AS NUMERIC)"
        return (
            f"{self.get_decimal_overflow_predicate_sql(quoted_column, whole_digits, 0)} "
            f"OR {number_sql} < {limit.lowest} OR {number_sql} > {limit.highest}"
        )

    def get_decimal_overflow_predicate_sql(self, quoted_column: str, max_digits: int, decimal_places: int) -> str:
        """A WHERE predicate matching rows whose numeric value doesn't fit
        ``DECIMAL(max_digits, decimal_places)`` - more fractional digits, or too many whole ones.

        Args:
            quoted_column: The quoted column holding an integer, float or decimal.
            max_digits: The digits the type holds.
            decimal_places: How many of them are fractional.

        Returns:
            The predicate.
        """
        number_sql = f"CAST({quoted_column} AS NUMERIC)"
        whole_digits_limit = "1" + "0" * (max_digits - decimal_places)
        return f"round({number_sql}, {decimal_places}) <> {number_sql} OR abs({number_sql}) >= {whole_digits_limit}"

    async def _alter_field(self, model: type[Model], old_field: Field[Any], new_field: Field[Any]) -> None:
        actions: list[str] = []
        dependent_generated_fields: list[Field[Any]] = []
        old_db_field = old_field.source_field or old_field.model_field_name
        new_db_field = new_field.source_field or new_field.model_field_name
        qualified_table = self._qualify_table_name(model._meta.db_table, model._meta.schema)
        if await self._alter_generated_field(model, old_field, new_field):
            return
        if old_db_field != new_db_field:
            # The rename runs first, at once - everything below refers to the column by its new
            # name.
            await self._run_sql(
                self.RENAME_FIELD_TEMPLATE.format(
                    table=qualified_table,
                    old_column=self.quote(old_db_field),
                    new_column=self.quote(new_db_field),
                )
            )
        old_sql_type = old_field.get_column_type(self.client.dialect)
        new_sql_type = new_field.get_column_type(self.client.dialect)
        if old_sql_type != new_sql_type:
            if self.client.dialect.truncates_values_on_type_change and not self.collect_sql:
                # A `::sql_type` cast truncates a too long string and rounds a too precise decimal
                # without an error - checked against the table's data before the ALTER runs.
                await self._check_alter_field_narrowing_data_loss(
                    qualified_table, old_field, new_field, self.quote(new_db_field)
                )
            # Postgres doesn't change the type of a column a generated column depends on - the
            # dependent generated columns are dropped and created again afterwards.
            dependent_generated_fields = self._get_generated_fields_depending_on_column(model, new_db_field)
            for generated_field in dependent_generated_fields:
                await self.remove_field(model, generated_field)
            changes = self.ALTER_FIELD_TYPE_TEMPLATE.format(column=self.quote(new_db_field), sql_type=new_sql_type)
            # The type change runs at once: the backfill below writes a value of the new type.
            await self._run_sql(self.ALTER_FIELD_TEMPLATE.format(table=qualified_table, changes=changes))
        if old_field.null != new_field.null:
            if new_field.null:
                changes = self.ALTER_FIELD_NULL_TEMPLATE.format(column=self.quote(new_db_field))
            else:
                # null=True -> null=False: existing NULL rows are filled with the field's default
                # before SET NOT NULL.
                backfill_value = self._field_backfill_sql_literal(new_field, model)
                if backfill_value is not None:
                    await self._run_sql(
                        f"UPDATE {qualified_table} SET {self.quote(new_db_field)} = {backfill_value} "  # nosec B608
                        f"WHERE {self.quote(new_db_field)} IS NULL"
                    )
                changes = self.ALTER_FIELD_NOT_NULL_TEMPLATE.format(column=self.quote(new_db_field))

            actions.append(self.ALTER_FIELD_TEMPLATE.format(table=qualified_table, changes=changes))

        old_indexed = old_field.index and not old_field.pk
        new_indexed = new_field.index and not new_field.pk
        if old_indexed != new_indexed:
            index = Index(fields=(new_db_field,))
            if new_indexed:
                await self.add_index(model, index)
            else:
                await self.remove_index(model, index)

        if old_field.unique != new_field.unique:
            constraint = UniqueConstraint(fields=(new_db_field,))
            if new_field.unique:
                await self.add_constraint(model, constraint)
            else:
                await self.remove_constraint(model, constraint)

        if old_field.description != new_field.description:
            await self._alter_column_comment(model, old_field, new_field)

        old_has_db_default = old_field.has_db_default()
        new_has_db_default = new_field.has_db_default()
        if old_has_db_default != new_has_db_default or (
            old_has_db_default and new_has_db_default and old_field.db_default != new_field.db_default
        ):
            if new_has_db_default:
                if hasattr(new_field.db_default, "get_sql"):
                    default_sql = new_field.db_default.get_sql(self.client.dialect)
                else:
                    db_val = self.client.dialect.types.get_db_value(new_field, new_field.db_default, model)
                    default_sql = self.client.dialect.get_literal_sql(db_val)
                changes = self.ALTER_FIELD_SET_DEFAULT_TEMPLATE.format(
                    column=self.quote(new_db_field), default=default_sql
                )
            else:
                changes = self.ALTER_FIELD_DROP_DEFAULT_TEMPLATE.format(column=self.quote(new_db_field))
            actions.append(self.ALTER_FIELD_TEMPLATE.format(table=qualified_table, changes=changes))

        if actions:
            result_query = ";\n".join(actions)
            await self._run_sql(result_query)

        for generated_field in dependent_generated_fields:
            await self.add_field(model, generated_field.model_field_name)

    @staticmethod
    def _is_same_model(first_model: type[Model] | None, second_model: type[Model]) -> bool:
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

    def _get_referencing_key_columns(
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
        target_pk_names = target_model._meta.pk_attr_names
        for candidate_model in candidate_models:
            for relation_field in candidate_model._meta.fields_map.values():
                if isinstance(relation_field, ForeignKeyFieldInstance):
                    if not self._is_same_model(relation_field.related_model, target_model):
                        continue
                    key_columns = tuple(
                        (
                            candidate_model._meta.fields_db_projection[key_field_name],
                            candidate_model._meta.fields_map[key_field_name].get_column_type(self.client.dialect),
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
                                self._qualify_table_name(candidate_model._meta.db_table, candidate_model._meta.schema),
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
                    if self._is_same_model(candidate_model, target_model):
                        side_keys.append(relation_field.backward_keys[component_index])
                    if self._is_same_model(relation_field.related_model, target_model):
                        side_keys.append(relation_field.forward_keys[component_index])
                    if side_keys:
                        column_type = target_pk_field.get_column_type(self.client.dialect)
                        referencing_key_columns.append(
                            ReferencingKeyColumns(
                                candidate_model,
                                relation_field,
                                self._qualify_table_name(relation_field.through, candidate_model._meta.schema),
                                tuple((key, column_type) for key in side_keys),
                            )
                        )
        return referencing_key_columns

    async def _alter_referenced_field(
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
            await self._drop_relation_foreign_keys(referencing.model, referencing.relation_field)
        await self._alter_field(model, old_field, new_field)
        for referencing in referencing_key_columns:
            for column, sql_type in referencing.key_columns:
                changes = self.ALTER_FIELD_TYPE_TEMPLATE.format(column=self.quote(column), sql_type=sql_type)
                await self._run_sql(
                    self.ALTER_FIELD_TEMPLATE.format(table=referencing.qualified_table, changes=changes)
                )
        for referencing in referencing_key_columns:
            await self.rebuild_relation_foreign_keys(referencing.model, referencing.relation_field)

    async def _drop_relation_foreign_keys(self, model: type[Model], relation_field: Field[Any]) -> None:
        """Drops the database FK constraint(s) behind a relation field, if any. No-op by default -
        SqliteSchemaEditor rebuilds whole tables instead of altering constraints in place."""

    async def _alter_composite_relation_index(
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
            await self.add_index(model, index)
        else:
            await self.remove_index(model, index)

    @staticmethod
    def get_altered_columns(
        old_model: type[Model], new_model: type[Model], field_name: str
    ) -> list[tuple[Field[Any], Field[Any]]]:
        """The columns an ``AlterField`` of a field changes, before and after.

        Args:
            old_model: The model rendered from the state before the change.
            new_model: The model rendered from the state after it.
            field_name: The altered field's name.

        Returns:
            The field itself for a plain column, each key column for a forward relation, nothing
            for a many-to-many relation - its through table holds its columns.
        """
        old_field = old_model._meta.fields_map[field_name]
        new_field = new_model._meta.fields_map[field_name]
        if isinstance(old_field, ManyToManyFieldInstance) or isinstance(new_field, ManyToManyFieldInstance):
            return []
        if isinstance(old_field, ForeignKeyFieldInstance) and isinstance(new_field, ForeignKeyFieldInstance):
            old_key_names = (
                old_field.source_fields
                if len(old_field.source_fields) > 1
                else (old_field.source_field or field_name,)
            )
            new_key_names = (
                new_field.source_fields
                if len(new_field.source_fields) > 1
                else (new_field.source_field or field_name,)
            )
            return [
                (old_model._meta.fields_map[old_key_name], new_model._meta.fields_map[new_key_name])
                for old_key_name, new_key_name in zip(old_key_names, new_key_names, strict=True)
            ]
        return [(old_field, new_field)]

    @classmethod
    def changes_column_type(
        cls, old_model: type[Model], new_model: type[Model], field_name: str, dialect: Dialect
    ) -> bool:
        """Whether an ``AlterField`` of a field changes the type of one of its columns - its values
        are converted, and a value the new type can't hold is lost.

        Args:
            old_model: The model rendered from the state before the change.
            new_model: The model rendered from the state after it.
            field_name: The altered field's name.
            dialect: The database's dialect.

        Returns:
            True when a column's type in the database changes.
        """
        return any(
            old_column.get_column_type(dialect) != new_column.get_column_type(dialect)
            for old_column, new_column in cls.get_altered_columns(old_model, new_model, field_name)
        )

    @classmethod
    def rewrites_table_on_alter(
        cls, old_model: type[Model], new_model: type[Model], field_name: str, dialect: Dialect
    ) -> bool:
        """Whether the database rewrites the whole table to alter a field - the time it takes grows
        with the table's rows. Changing a column's type does.

        Args:
            old_model: The model rendered from the state before the change.
            new_model: The model rendered from the state after it.
            field_name: The altered field's name.
            dialect: The database's dialect.

        Returns:
            True when the table is rewritten.
        """
        return cls.changes_column_type(old_model, new_model, field_name, dialect)

    async def alter_field(
        self,
        old_model: type[Model],
        new_model: type[Model],
        field_name: str,
        candidate_referencing_models: Sequence[type[Model]] = (),
    ) -> None:
        """Alters one field's column (or M2M through table) from its old to its new definition.

        Args:
            old_model: The model rendered from the state the database currently matches.
            new_model: The model rendered from the target state.
            field_name: The altered field's name.
            candidate_referencing_models: Models of the target state that may hold relations to
                `new_model` - a changed column type is carried over to their key columns.
        """
        old_field = old_model._meta.fields_map[field_name]
        new_field = new_model._meta.fields_map[field_name]

        old_is_relation = isinstance(old_field, (ManyToManyFieldInstance, ForeignKeyFieldInstance))
        new_is_relation = isinstance(new_field, (ManyToManyFieldInstance, ForeignKeyFieldInstance))
        if old_is_relation != new_is_relation:
            # A relation field and a plain column can't be converted into each other by an ALTER.
            raise ConfigurationError(
                f"Converting field '{field_name}' between a relation field and a plain column "
                "is not supported by AlterField - remove the field and add it back instead."
            )

        if isinstance(old_field, ManyToManyFieldInstance):
            new_field = cast("ManyToManyFieldInstance[Model]", new_field)
            await self._alter_m2m_field(new_model, old_field, new_field)
            return

        if isinstance(old_field, ForeignKeyFieldInstance):
            new_field = cast("ForeignKeyFieldInstance[Model]", new_field)
            old_source = old_field.source_field or field_name
            new_source = new_field.source_field or field_name
            # on_delete and db_constraint live on the relation field - compared here, before the key
            # columns take its place below. The column still has its old name at this point.
            # One key column per component of a composite target - each is altered on its own.
            old_key_names = old_field.source_fields if len(old_field.source_fields) > 1 else (old_source,)
            new_key_names = new_field.source_fields if len(new_field.source_fields) > 1 else (new_source,)
            key_field_pairs = [
                (old_model._meta.fields_map[old_key_name], new_model._meta.fields_map[new_key_name])
                for old_key_name, new_key_name in zip(old_key_names, new_key_names, strict=True)
            ]
            old_target_columns = self._get_relation_target_columns(old_field)
            target_changed = old_target_columns != self._get_relation_target_columns(new_field)
            key_type_changed = any(
                old_key_field.get_column_type(self.client.dialect)
                != new_key_field.get_column_type(self.client.dialect)
                for old_key_field, new_key_field in key_field_pairs
            )
            if target_changed or key_type_changed:
                # The constraint can't survive the key column switching to another column's
                # values/type - dropped first, rebuilt against the new target once it has.
                if target_changed:
                    await self._raise_if_relation_values_need_remapping(old_model, old_field, new_field)
                await self._drop_relation_foreign_keys(old_model, old_field)
                for old_key_field, new_key_field in key_field_pairs:
                    await self._alter_field(new_model, old_key_field, new_key_field)
                await self._restore_relation_foreign_keys(new_model, new_field)
                await self._alter_composite_relation_index(new_model, old_field, new_field)
                return
            if self._foreign_key_changed(old_field, new_field):
                await self._alter_fk_on_delete(new_model, old_source, old_field, new_field)
            for old_key_field, new_key_field in key_field_pairs:
                await self._alter_field(new_model, old_key_field, new_key_field)
            await self._alter_composite_relation_index(new_model, old_field, new_field)
            return

        referencing_key_columns: list[ReferencingKeyColumns] = []
        old_sql_type = old_field.get_column_type(self.client.dialect)
        if candidate_referencing_models and old_sql_type != new_field.get_column_type(self.client.dialect):
            referencing_key_columns = self._get_referencing_key_columns(
                new_model, field_name, candidate_referencing_models
            )
        if referencing_key_columns:
            await self._alter_referenced_field(new_model, old_field, new_field, referencing_key_columns)
            return
        await self._alter_field(new_model, old_field, new_field)

    @staticmethod
    def _get_relation_target_columns(
        fk_field: ForeignKeyFieldInstance[Model],
    ) -> tuple[str | None, str, tuple[str, ...]]:
        """The schema, table and columns a FK/O2O field's values refer to.

        Args:
            fk_field: A FK/O2O field of a rendered state model.

        Returns:
            The target's schema, table and referenced column names.
        """
        related_meta = fk_field.related_model._meta
        return (
            related_meta.schema,
            related_meta.db_table,
            tuple(field.source_field or field.model_field_name for field in fk_field.to_field_instances),
        )

    async def _raise_if_relation_values_need_remapping(
        self,
        model: type[Model],
        old_fk_field: ForeignKeyFieldInstance[Model],
        new_fk_field: ForeignKeyFieldInstance[Model],
    ) -> None:
        """Refuses to repoint a FK/O2O at another target column while rows still store key values.

        Stored values name rows through the old target column - kept as they are, they'd silently
        point at the wrong rows (or none) through the new one.

        Args:
            model: The model owning the relation, rendered from the current state.
            old_fk_field: The relation's current definition.
            new_fk_field: The relation's new definition.

        Raises:
            ForeignKeyTargetChangeError: At least one row stores a key value.
        """
        if self.collect_sql:
            return
        columns = [model._meta.fields_db_projection[key_field_name] for key_field_name in old_fk_field.source_fields]
        qualified_table = self._qualify_table_name(model._meta.db_table, model._meta.schema)
        predicate = " OR ".join(f"{self.quote(column)} IS NOT NULL" for column in columns)
        rows = await self.client.execute_dicts(
            f"SELECT count(*) AS stored_count FROM {qualified_table} WHERE {predicate}"  # nosec B608
        )
        stored_count = rows[0]["stored_count"]
        if not stored_count:
            return
        _old_schema, old_table, old_columns = self._get_relation_target_columns(old_fk_field)
        _new_schema, new_table, new_columns = self._get_relation_target_columns(new_fk_field)
        raise ForeignKeyTargetChangeError(
            f"Cannot repoint {model.__name__}.{old_fk_field.model_field_name} from "
            f"{old_table}({', '.join(old_columns)}) to {new_table}({', '.join(new_columns)}) - "
            f"{stored_count} row(s) store values of the old target column, which would silently "
            "refer to the wrong rows (or none) afterwards. Migrate the data by hand instead: add a "
            "new relation field targeting the new column, fill it with RunPython/RunSQL, then "
            "remove the old field."
        )

    async def _restore_relation_foreign_keys(self, model: type[Model], relation_field: Field[Any]) -> None:
        """Re-creates the FK constraint(s) of a relation whose key column was just altered.

        Args:
            model: The model owning `relation_field`, rendered from the new state.
            relation_field: The altered relation field.
        """
        await self.rebuild_relation_foreign_keys(model, relation_field)

    async def _alter_fk_on_delete(
        self,
        model: type[Model],
        db_field: str,
        old_fk_field: ForeignKeyFieldInstance[Model],
        new_fk_field: ForeignKeyFieldInstance[Model],
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
        if not self.client.dialect.supports_foreign_keys:
            return
        if isinstance(field, ManyToManyFieldInstance):
            if field._generated or field.through_model is not None:
                return
            await self._rebuild_m2m_through_foreign_keys(model, field)
            return
        if isinstance(field, ForeignKeyFieldInstance):
            await self._rebuild_fk_foreign_keys(model, field)

    async def _rebuild_fk_foreign_keys(self, model: type[Model], fk_field: ForeignKeyFieldInstance[Model]) -> None:
        """Replaces a FK/O2O field's own constraint - see rebuild_relation_foreign_keys()."""
        await self._alter_fk_on_delete(model, fk_field.source_fields[0], fk_field, fk_field)

    async def _rebuild_m2m_through_foreign_keys(
        self, model: type[Model], field: ManyToManyFieldInstance[Model]
    ) -> None:
        """Replaces both FK constraints of an auto-managed M2M through table - see
        rebuild_relation_foreign_keys().

        Raises:
            UnSupportedError: On a dialect without its own implementation.
        """
        raise UnSupportedError(
            f"Rebuilding M2M through-table constraints is not supported on {self.client.dialect.name}"
        )

    async def remove_field(self, model: type[Model], field: Field[Any]) -> None:
        if isinstance(field, ManyToManyFieldInstance):
            if field.through_model is not None:
                # SomeModel (through=SomeModel) keeps its own table - dropped via its own
                # DropModel operation, not by removing this M2M field.
                return
            await self._run_sql(
                self.DELETE_TABLE_TEMPLATE.format(table=self._qualify_table_name(field.through, model._meta.schema))
            )
            return

        if isinstance(field, ForeignKeyFieldInstance) and len(field.source_fields) > 1:
            # Composite target: every key column is dropped - the database drops the constraint with
            # its columns.
            for source_field_name in field.source_fields:
                shadow_field = model._meta.fields_map[source_field_name]
                db_field = model._meta.fields_db_projection.get(
                    shadow_field.model_field_name, shadow_field.source_field or shadow_field.model_field_name
                )
                await self._run_sql(
                    self.DELETE_FIELD_TEMPLATE.format(
                        table=self._qualify_table_name(model._meta.db_table, model._meta.schema),
                        column=self.quote(db_field),
                    )
                )
            return

        if isinstance(field, ForeignKeyFieldInstance):
            source_field = field.source_field or field.model_field_name
            field = model._meta.fields_map[source_field]
            # No explicit constraint drop needed here - see the composite-target branch above
            # for why (verified against Postgres: this column's own FK/CHECK/UNIQUE/index all
            # get dropped automatically along with it).
        db_field = model._meta.fields_db_projection.get(
            field.model_field_name, field.source_field or field.model_field_name
        )
        await self._run_sql(
            self.DELETE_FIELD_TEMPLATE.format(
                table=self._qualify_table_name(model._meta.db_table, model._meta.schema),
                column=self.quote(db_field),
            )
        )

    def _column_names_for_index(self, model: type[Model], index: Index) -> list[str]:
        """Returns the index's keys as this connection's ``CREATE INDEX`` lists them - a field's
        own column (its ``source_field``), an expression rendered by the connection's dialect.

        Args:
            model: The indexed model.
            index: The index.

        Returns:
            One key each.
        """
        return index.get_key_sqls(model, self.client.dialect)

    def _index_name_for_model(self, model: type[Model], index: Index) -> str:
        if index.name:
            return index.name
        index.get_expressions(model)
        # Named after the keys in plain SQL - the same name on every database.
        column_names = (
            self._get_fields_to_columns(model, index.field_names) if index.fields else list(index.field_names)
        )
        return GeneratedNames.get_index_name(
            GeneratedNamePrefix.UNIQUE_INDEX if index.unique else GeneratedNamePrefix.INDEX,
            model,
            column_names,
            index.get_name_parts(),
        )

    def _get_index_create_sql(
        self,
        model: type[Model],
        index: Index,
        index_name: str | None = None,
        indexed_table_sql: str | None = None,
    ) -> str:
        """Returns the statement creating an index.

        Args:
            model: The indexed model.
            index: The index.
            index_name: The index's name, its own by default.
            indexed_table_sql: The table the index is created on, the model's by default.

        Returns:
            The statement.
        """
        index.get_expressions(model)
        return self._get_index_sql(
            model,
            self._column_names_for_index(model, index),
            index_name=index_name or self._index_name_for_model(model, index),
            index_type=index.INDEX_TYPE,
            extra=index.get_extra(model, self.client),
            opclasses=index.opclasses or None,
            unique=index.unique,
            orders=index.field_orders or None,
            indexed_table_sql=indexed_table_sql,
        )

    def _get_index_drop_sql(self, model: type[Model], index: Index) -> str:
        """Returns the statement dropping an index.

        Args:
            model: The indexed model.
            index: The index.

        Returns:
            The statement.
        """
        index_name = self._index_name_for_model(model, index)
        return self.DROP_INDEX_TEMPLATE.format(name=self._qualify_table_name(index_name, model._meta.schema))

    async def add_index(self, model: type[Model], index: Index, concurrently: bool = False) -> None:
        """Creates an index.

        Args:
            model: The indexed model.
            index: The index.
            concurrently: Build it without blocking writes - on a dialect that can
                (``Dialect.supports_concurrent_indexes``); elsewhere it is built the plain way.
        """
        index_sql = self._get_index_create_sql(model, index)
        if index_sql:
            await self._run_sql(index_sql)

    async def remove_index(self, model: type[Model], index: Index, concurrently: bool = False) -> None:
        """Drops an index.

        Args:
            model: The indexed model.
            index: The index.
            concurrently: Drop it without blocking reads and writes - on a dialect that can
                (``Dialect.supports_concurrent_indexes``); elsewhere it is dropped the plain way.
        """
        await self._run_sql(self._get_index_drop_sql(model, index))

    def _get_generated_index_names(self, model: type[Model]) -> dict[tuple[str, Any], tuple[Index, bool]]:
        """Every index or unique constraint of `model` whose name is generated from its table.

        Args:
            model: The model.

        Returns:
            A key stable across a table rename -> (an Index carrying the generated name, whether
            it backs a unique constraint rather than being a plain index).
        """
        generated: dict[tuple[str, Any], tuple[Index, bool]] = {}
        for field_name, field_column_names in model._meta.get_field_index_columns():
            generated[("field", field_name)] = (
                Index(
                    fields=(field_name,),
                    name=GeneratedNames.get_index_name(GeneratedNamePrefix.INDEX, model, field_column_names),
                ),
                False,
            )
        for position, entry in enumerate(model._meta.indexes or ()):
            declared_index = entry if isinstance(entry, Index) else Index(fields=tuple(entry))
            if declared_index.name:
                continue
            named_index = copy(declared_index)
            named_index.name = self._index_name_for_model(model, declared_index)
            generated[("index", position)] = (named_index, False)
        for position, constraint in enumerate(model._meta.constraints or ()):
            if not isinstance(constraint, UniqueConstraint) or constraint.name:
                continue
            column_names = model._meta.get_column_names(constraint.fields)
            generated[("constraint", position)] = (
                Index(
                    fields=tuple(constraint.fields),
                    unique=True,
                    name=self._constraint_name_for_model(model, UniqueConstraint(fields=tuple(column_names))),
                ),
                True,
            )
        return generated

    async def rename_generated_index_names(self, old_model: type[Model], new_model: type[Model]) -> None:
        """Renames the indexes and unique constraints named after a table that was just renamed,
        so they keep the names a later migration computes for them.

        Args:
            old_model: The model under its old table name.
            new_model: The model under its new table name - its table already renamed.
        """
        new_generated = self._get_generated_index_names(new_model)
        for key, (old_index, is_constraint) in self._get_generated_index_names(old_model).items():
            new_entry = new_generated.get(key)
            if new_entry is None or new_entry[0].name == old_index.name:
                continue
            await self._rename_generated_index(new_model, old_index, new_entry[0], is_constraint)

    async def _rename_generated_index(
        self, model: type[Model], old_index: Index, new_index: Index, is_constraint: bool
    ) -> None:
        """Renames one index with a generated name; a missing one is left alone.

        Args:
            model: The model under its new table name.
            old_index: The index under its old generated name.
            new_index: The index under its new generated name.
            is_constraint: Whether the index backs a unique constraint.
        """
        if self.RENAME_INDEX_IF_EXISTS_TEMPLATE is None:
            await self.rename_index(model, old_index, new_index)
            return
        await self._run_sql(
            self.RENAME_INDEX_IF_EXISTS_TEMPLATE.format(
                old_name=self._qualify_table_name(cast("str", old_index.name), model._meta.schema),
                new_name=self.quote(cast("str", new_index.name)),
            )
        )

    async def rename_index(self, model: type[Model], old_index: Index, new_index: Index) -> None:
        old_name = self._index_name_for_model(model, old_index)
        new_name = self._index_name_for_model(model, new_index)
        if old_name == new_name:
            return
        if self.RENAME_INDEX_TEMPLATE:
            await self._run_sql(
                self.RENAME_INDEX_TEMPLATE.format(
                    old_name=self._qualify_table_name(old_name, model._meta.schema),
                    new_name=self.quote(new_name),
                )
            )
            return
        await self.remove_index(model, old_index)
        await self.add_index(model, new_index)

    def _constraint_name_for_model(self, model: type[Model], constraint: UniqueConstraint) -> str:
        """The ``uid_<table>_<field>_<hash>`` name of a unique constraint added to an existing table -
        made from the table and field names alone, so it is the same wherever the constraint is
        added, removed or renamed.
        """
        if constraint.name:
            return constraint.name
        return self._get_unique_constraint_name(model, list(constraint.fields))

    def _get_fields_to_columns(self, model: type[Model], field_names: tuple[str, ...] | list[str]) -> list[str]:
        """Returns the database column names of model field names - a relation's key column
        (``organization`` -> ``organization_id``). A name that isn't a field of the model is
        returned as is.
        """
        return model._meta.get_column_names(field_names)

    async def _get_unique_constraint_names_from_db(
        self, table_name: str, column_names: list[str], schema: str | None = None
    ) -> list[str]:
        """The names of the unique constraints over exactly ``column_names``, in that order, read from
        the database. Empty by default.
        """
        return []

    async def _get_constraint_name(self, model: type[Model], constraint: UniqueConstraint) -> str:
        """The constraint's name in the database when introspection finds it (a legacy database may
        have named it differently), else the deterministic ``uid_`` name.
        """
        constraint_column_names = self._get_fields_to_columns(model, constraint.fields)
        column_constraint = UniqueConstraint(fields=tuple(constraint_column_names), name=constraint.name)
        deterministic_name = self._constraint_name_for_model(model, column_constraint)
        if not self.collect_sql:
            try:
                introspected = await self._get_unique_constraint_names_from_db(
                    model._meta.db_table, constraint_column_names, model._meta.schema
                )
                if introspected:
                    return introspected[0]
            except Exception:  # nosec B110
                # Introspection unavailable (FakeClient, no connection, etc.)
                # Fall back to deterministic name
                pass
        return deterministic_name

    def _check_unique_constraint_supported(self, constraint: UniqueConstraint) -> None:
        """Rejects a unique constraint the dialect has no DDL for.

        Args:
            constraint: The unique constraint.

        Raises:
            ConfigurationError: It is deferrable with a condition (a partial unique constraint is
                an index, and an index can't be deferred).
            UnSupportedError: It has a condition on a dialect without partial indexes, is
                deferrable on a dialect without deferrable constraints, or sets
                ``nulls_distinct`` the connection's server has no syntax for.
        """
        dialect = self.client.dialect
        if constraint.condition and not dialect.supports_partial_indexes:
            raise UnSupportedError(f"Partial unique indexes (condition) are not supported on {dialect}")
        if constraint.deferrable and constraint.condition:
            raise ConfigurationError("UniqueConstraint.deferrable is not supported together with condition.")
        if constraint.deferrable and not dialect.supports_deferrable_constraints:
            raise UnSupportedError(f"DEFERRABLE unique constraints are not supported on {dialect}")
        constraint.raise_if_unsupported(self.client.features, dialect)

    def _get_partial_unique_index_name(self, model: type[Model], constraint: UniqueConstraint) -> str:
        """Returns the name of the unique index a unique constraint with a condition is created as.

        Args:
            model: The constrained model.
            constraint: The unique constraint.

        Returns:
            The index name.
        """
        column_constraint = UniqueConstraint(
            fields=tuple(self._get_fields_to_columns(model, constraint.fields)),
            name=constraint.name,
            condition=constraint.condition,
        )
        return self._constraint_name_for_model(model, column_constraint)

    def _exclusion_constraint_sql(self, model: type[Model], constraint: ExclusionConstraint) -> str:
        """Returns the definition of an exclusion constraint.

        Args:
            model: The constrained model.
            constraint: The constraint.

        Returns:
            The definition, as it follows ``ADD`` and stands in ``CREATE TABLE``.

        Raises:
            UnSupportedError: The dialect has no exclusion constraints.
        """
        raise UnSupportedError(f"ExclusionConstraint is not supported on {self.client.dialect}")

    async def add_check_constraint_not_valid(self, model: type[Model], constraint: CheckConstraint) -> None:
        """Adds a CHECK constraint that only new and updated rows must pass - the existing rows are
        checked later, by ``validate_constraint()``. A dialect that can't leave the existing rows
        unchecked (``Dialect.supports_not_valid_constraints`` False) adds the constraint as usual,
        which checks every row.

        Args:
            model: The constrained model.
            constraint: The check constraint.
        """
        await self.add_constraint(model, constraint)

    async def validate_constraint(self, model: type[Model], name: str) -> None:
        """Checks the existing rows against a constraint added without checking them; nothing to
        do where every constraint is checked as it is added.

        Args:
            model: The constrained model.
            name: The constraint's name.
        """

    async def add_constraint(
        self,
        model: type[Model],
        constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint | ForeignKeyConstraint,
    ) -> None:
        if isinstance(constraint, ExclusionConstraint):
            await self._run_sql(
                self.ADD_CONSTRAINT_TEMPLATE.format(
                    table=self._qualify_table_name(model._meta.db_table, model._meta.schema),
                    constraint=self._exclusion_constraint_sql(model, constraint),
                )
            )
            return
        if isinstance(constraint, ForeignKeyConstraint):
            constraint_sql = self.FOREIGN_KEY_CONSTRAINT_CREATE_TEMPLATE.format(
                name=self.quote(constraint.name),
                fields=", ".join(self.quote(f) for f in constraint.fields),
                table=constraint.to_table,
                to_fields=", ".join(self.quote(f) for f in constraint.to_fields),
                on_delete=constraint.on_delete,
            )
            await self._run_sql(
                self.ADD_CONSTRAINT_TEMPLATE.format(
                    table=self._qualify_table_name(model._meta.db_table, model._meta.schema),
                    constraint=constraint_sql,
                )
            )
            return
        if isinstance(constraint, CheckConstraint):
            constraint_sql = self.CHECK_CONSTRAINT_CREATE_TEMPLATE.format(
                name=self.quote(constraint.name),
                check=ConstraintCondition.get_sql(constraint.check, model, self.client),
            )
            await self._run_sql(
                self.ADD_CONSTRAINT_TEMPLATE.format(
                    table=self._qualify_table_name(model._meta.db_table, model._meta.schema),
                    constraint=constraint_sql,
                )
            )
            return
        if not self.client.dialect.supports_unique_constraints:
            return
        self._check_unique_constraint_supported(constraint)
        constraint_column_names = self._get_fields_to_columns(model, constraint.fields)
        if constraint.condition:
            # A partial unique constraint is a unique index with a WHERE clause - an index takes a
            # condition, a table constraint doesn't.
            index_sql = (
                f"CREATE UNIQUE INDEX {self.quote(self._get_partial_unique_index_name(model, constraint))} "
                f"ON {self._qualify_table_name(model._meta.db_table, model._meta.schema)} "
                f"({', '.join([self.quote(f) for f in constraint_column_names])})"
                f"{constraint.get_include_sql(model, self.client.dialect, self.quote)}"
                f"{constraint.get_nulls_sql(self.client.dialect)} "
                f"WHERE {ConstraintCondition.get_sql(constraint.condition, model, self.client)}"
            )
            await self._run_sql(index_sql + ";")
            return
        constraint_name = self._constraint_name_for_model(
            model, UniqueConstraint(fields=tuple(constraint_column_names), name=constraint.name)
        )
        constraint_sql = self.UNIQUE_CONSTRAINT_CREATE_TEMPLATE.format(
            index_name=self.quote(constraint_name),
            nulls=constraint.get_nulls_sql(self.client.dialect),
            fields=", ".join([self.quote(f) for f in constraint_column_names]),
            include=constraint.get_include_sql(model, self.client.dialect, self.quote),
        )
        if isinstance(constraint, UniqueConstraint) and constraint.deferrable:
            constraint_sql += " DEFERRABLE INITIALLY " + ("DEFERRED" if constraint.initially_deferred else "IMMEDIATE")
        await self._run_sql(
            self.ADD_CONSTRAINT_TEMPLATE.format(
                table=self._qualify_table_name(model._meta.db_table, model._meta.schema),
                constraint=constraint_sql,
            )
        )

    async def remove_constraint(
        self,
        model: type[Model],
        constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint | ForeignKeyConstraint,
    ) -> None:
        if isinstance(constraint, (CheckConstraint, ExclusionConstraint, ForeignKeyConstraint)):
            await self._run_sql(
                self.DELETE_CONSTRAINT_TEMPLATE.format(
                    table=self._qualify_table_name(model._meta.db_table, model._meta.schema),
                    name=self.quote(constraint.name),
                )
            )
            return
        if not self.client.dialect.supports_unique_constraints:
            return
        if constraint.condition:
            # A partial unique constraint is an index - see add_constraint().
            await self._run_sql(
                self.DROP_INDEX_TEMPLATE.format(
                    name=self._qualify_table_name(
                        self._get_partial_unique_index_name(model, constraint), model._meta.schema
                    )
                )
            )
            return
        constraint_name = await self._get_constraint_name(model, constraint)
        await self._run_sql(
            self.DELETE_CONSTRAINT_TEMPLATE.format(
                table=self._qualify_table_name(model._meta.db_table, model._meta.schema),
                name=self.quote(constraint_name),
            )
        )

    async def rename_constraint(
        self,
        model: type[Model],
        old_constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint,
        new_constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint,
    ) -> None:
        if isinstance(old_constraint, UniqueConstraint) and not self.client.dialect.supports_unique_constraints:
            return
        if isinstance(old_constraint, UniqueConstraint) and old_constraint.condition:
            await self._rename_partial_unique_index(model, old_constraint, new_constraint)
            return
        # For CheckConstraint/ExclusionConstraint or any named constraint, use names directly
        if isinstance(old_constraint, (CheckConstraint, ExclusionConstraint)):
            if type(new_constraint) is not type(old_constraint):
                raise TypeError(f"Cannot rename {type(old_constraint).__name__} to {type(new_constraint).__name__}")
            old_name = old_constraint.name
            new_name = cast("CheckConstraint | ExclusionConstraint", new_constraint).name
        else:
            if not isinstance(new_constraint, UniqueConstraint):
                raise TypeError(f"Cannot rename UniqueConstraint to {type(new_constraint).__name__}")
            # The old name is looked up in the database, as remove_constraint() does; the new one
            # doesn't exist yet.
            old_name = await self._get_constraint_name(model, old_constraint)
            new_column_names = self._get_fields_to_columns(model, new_constraint.fields)
            new_c = UniqueConstraint(fields=tuple(new_column_names), name=new_constraint.name)
            new_name = self._constraint_name_for_model(model, new_c)
        if old_name == new_name:
            return
        if self.RENAME_CONSTRAINT_TEMPLATE:
            await self._run_sql(
                self.RENAME_CONSTRAINT_TEMPLATE.format(
                    table=self._qualify_table_name(model._meta.db_table, model._meta.schema),
                    old_name=self.quote(old_name),
                    new_name=self.quote(new_name),
                )
            )
            return
        await self.remove_constraint(model, old_constraint)
        await self.add_constraint(model, new_constraint)

    async def _rename_partial_unique_index(
        self,
        model: type[Model],
        old_constraint: UniqueConstraint,
        new_constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint,
    ) -> None:
        """Renames the unique index a unique constraint with a condition is - a partial unique
        constraint has no table constraint to rename (see ``add_constraint()``). Without
        ``ALTER INDEX ... RENAME`` the index is dropped and created again under the new name.

        Args:
            model: The constrained model.
            old_constraint: The constraint's old definition.
            new_constraint: Its new definition.

        Raises:
            TypeError: The new constraint isn't a unique constraint.
        """
        if not isinstance(new_constraint, UniqueConstraint):
            raise TypeError(f"Cannot rename UniqueConstraint to {type(new_constraint).__name__}")
        old_name = self._get_partial_unique_index_name(model, old_constraint)
        new_name = self._get_partial_unique_index_name(model, new_constraint)
        if old_name == new_name:
            return
        if self.RENAME_INDEX_TEMPLATE:
            await self._run_sql(
                self.RENAME_INDEX_TEMPLATE.format(
                    old_name=self._qualify_table_name(old_name, model._meta.schema), new_name=self.quote(new_name)
                )
            )
            return
        await self._run_sql(
            self.DROP_INDEX_TEMPLATE.format(name=self._qualify_table_name(old_name, model._meta.schema))
        )
        await self.add_constraint(model, new_constraint)

    def get_trigger_create_sqls(self, model: type[Model], trigger: Trigger, safe: bool = False) -> list[str]:
        """The statements creating a trigger - shared by add_trigger() and generate_schemas().

        Args:
            model: The model the trigger is declared on.
            trigger: The trigger.
            safe: Replace an existing trigger of the same name instead of failing.

        Returns:
            The DDL statements, in execution order.

        Raises:
            UnSupportedError: The dialect has no triggers.
        """
        raise UnSupportedError(f"Triggers are not supported on {self.client.dialect}")

    async def add_trigger(self, model: type[Model], trigger: Trigger) -> None:
        for statement in self.get_trigger_create_sqls(model, trigger):
            await self._run_sql(statement)

    async def remove_trigger(self, model: type[Model], trigger: Trigger) -> None:
        """Drops a trigger.

        Raises:
            UnSupportedError: The dialect has no triggers.
        """
        raise UnSupportedError(f"Triggers are not supported on {self.client.dialect}")

    async def alter_trigger(self, model: type[Model], old_trigger: Trigger, new_trigger: Trigger) -> None:
        await self.remove_trigger(model, old_trigger)
        await self.add_trigger(model, new_trigger)

    async def rename_trigger(self, model: type[Model], old_trigger: Trigger, new_trigger: Trigger) -> None:
        if old_trigger.name == new_trigger.name:
            return
        await self.remove_trigger(model, old_trigger)
        await self.add_trigger(model, new_trigger)

    async def create_schema(self, schema_name: str) -> None:
        await self._run_sql(f"CREATE SCHEMA IF NOT EXISTS {self.quote(schema_name)};")

    async def drop_schema(self, schema_name: str) -> None:
        await self._run_sql(f"DROP SCHEMA IF EXISTS {self.quote(schema_name)} CASCADE;")

    async def move_table_to_schema(self, table_name: str, old_schema: str | None, new_schema: str | None) -> None:
        """Moves an existing table to another schema, with its rows, constraints and indexes.

        Args:
            table_name: The table.
            old_schema: The schema it is in; None for the connection's current schema.
            new_schema: The schema it moves to; None for the connection's current schema.

        Raises:
            UnSupportedError: The dialect can't move a table between schemas.
        """
        raise UnSupportedError(f"Moving a table to another schema is not supported on {self.client.dialect}")

    async def create_extension(self, extension_name: str) -> None:
        """Installs a database extension, unless it is installed.

        Args:
            extension_name: The extension's name.

        Raises:
            UnSupportedError: The dialect has no extensions.
        """
        raise UnSupportedError(f"Database extensions are not supported on {self.client.dialect}")

    async def drop_extension(self, extension_name: str) -> None:
        """Removes a database extension, if it is installed.

        Args:
            extension_name: The extension's name.

        Raises:
            UnSupportedError: The dialect has no extensions.
        """
        raise UnSupportedError(f"Database extensions are not supported on {self.client.dialect}")

    async def create_collation(self, name: str, locale: str, provider: str, deterministic: bool) -> None:
        """Creates a collation.

        Args:
            name: The collation's name.
            locale: Its locale, e.g. ``"und-u-ks-level2"``.
            provider: ``"libc"`` or ``"icu"``.
            deterministic: False for a collation equal strings can differ under.

        Raises:
            UnSupportedError: The dialect has no collation DDL.
        """
        raise UnSupportedError(f"Creating a collation is not supported on {self.client.dialect}")

    async def drop_collation(self, name: str) -> None:
        """Drops a collation.

        Args:
            name: The collation's name.

        Raises:
            UnSupportedError: The dialect has no collation DDL.
        """
        raise UnSupportedError(f"Dropping a collation is not supported on {self.client.dialect}")

    @staticmethod
    def _get_remake_value_conversion_sql(old_field: Field[Any], new_field: Field[Any], quoted_column: str) -> str:
        """The expression copying a column's values into a rebuilt table whose field changed type.

        Args:
            old_field: The field's previous definition.
            new_field: The field's new definition.
            quoted_column: The quoted column of the table being replaced.

        Returns:
            The column itself - the database converts a stored value to the new column type.
        """
        return quoted_column

    async def _replace_table(
        self, table_name: str, rebuilt_table_name: str, schema: str | None, fields: Iterable[Field[Any]]
    ) -> None:
        """Drops a table and renames its rebuilt copy into its place.

        Args:
            table_name: The table being replaced.
            rebuilt_table_name: The already filled copy taking its place.
            schema: The tables' schema.
            fields: The fields of the rebuilt table's columns.
        """
        qualified_table = self._qualify_table_name(table_name, schema)
        await self._run_sql(f"DROP TABLE {qualified_table}")
        await self._run_sql(
            f"ALTER TABLE {self._qualify_table_name(rebuilt_table_name, schema)} RENAME TO {self.quote(table_name)}"
        )

    @staticmethod
    def _get_remake_column_name(field: Field[Any]) -> str:
        """The DB column a field occupies.

        Args:
            field: A field of a rendered model.

        Returns:
            The column name - for a single-column FK/O2O its key column, since the relation
            field's own source_field names the shadow attribute rather than the column.
        """
        if isinstance(field, ForeignKeyFieldInstance) and len(field.db_column_names) == 1:
            return field.db_column_names[0]
        return field.source_field or field.model_field_name

    def _build_remake_column_mapping(
        self, model, create_field=None, delete_field=None, alter_fields=None
    ) -> dict[str, str]:
        """Maps each surviving column's NEW db name to the SQL expression that copies (or
        backfills) it from the old table - _remake_table()'s own INSERT...SELECT step reads this
        directly."""
        alter_fields = alter_fields or []
        column_mapping = {}
        for field in model._meta.fields_map.values():
            if isinstance(field, (ManyToManyFieldInstance, BackwardFKRelation, BackwardOneToOneRelation)):
                continue
            db_field = self._get_remake_column_name(field)
            if self._get_non_pk_generated_field_sql(field, db_field) is not None:
                # A generated column takes no value in an INSERT - left out of the copy.
                continue
            column_mapping[db_field] = self.quote(db_field)

        if create_field:
            if not isinstance(
                create_field,
                (ManyToManyFieldInstance, BackwardFKRelation, BackwardOneToOneRelation),
            ):
                if isinstance(create_field, ForeignKeyFieldInstance) and len(create_field.source_fields) > 1:
                    # Composite target: every key column is new - filled with NULL.
                    for db_field in create_field.source_fields:
                        column_mapping[db_field] = "NULL"
                else:
                    db_field = self._get_remake_column_name(create_field)
                    # A new generated column takes no value either.
                    if self._get_non_pk_generated_field_sql(create_field, db_field) is None:
                        default_val = self._field_backfill_sql_literal(create_field, model)
                        column_mapping[db_field] = default_val if default_val is not None else "NULL"

        for old_field, new_field in alter_fields:
            old_db_field = self._get_remake_column_name(old_field)
            new_db_field = self._get_remake_column_name(new_field)
            column_mapping.pop(old_db_field, None)

            # An altered generated column stays out of the column list.
            if self._get_non_pk_generated_field_sql(new_field, new_db_field) is not None:
                continue

            if old_field.null and not new_field.null:
                default_val = self._field_backfill_sql_literal(new_field, model)
            else:
                default_val = None
            copied_value_sql = self._get_remake_value_conversion_sql(old_field, new_field, self.quote(old_db_field))
            if default_val is not None:
                column_mapping[new_db_field] = f"COALESCE({copied_value_sql}, {default_val})"
            else:
                column_mapping[new_db_field] = copied_value_sql

        if delete_field:
            if not isinstance(delete_field, ManyToManyFieldInstance):
                if isinstance(delete_field, ForeignKeyFieldInstance) and len(delete_field.source_fields) > 1:
                    for db_field in delete_field.source_fields:
                        column_mapping.pop(db_field, None)
                else:
                    column_mapping.pop(self._get_remake_column_name(delete_field), None)

        return column_mapping

    def _get_remake_fields_by_db_column(
        self, model, create_field=None, delete_field=None, alter_fields=None
    ) -> dict[str, Field[Any]]:
        """Maps each surviving column's NEW db name to the field object describing its NEW
        definition (post add/alter/delete) - what _build_remake_field_definitions() below
        actually renders a column definition from."""
        alter_fields = alter_fields or []
        fields_by_db_column = {}
        if isinstance(delete_field, ForeignKeyFieldInstance):
            # The relation field being deleted is no column itself - its key fields are fields of
            # their own.
            delete_shadow_field_names = (
                set(delete_field.source_fields)
                if len(delete_field.source_fields) > 1
                else {delete_field.source_field or delete_field.model_field_name}
            )
        else:
            delete_shadow_field_names = set()
        for field in model._meta.fields_map.values():
            if isinstance(field, (ManyToManyFieldInstance, BackwardFKRelation, BackwardOneToOneRelation)):
                continue
            if not hasattr(field, "get_column_type"):
                continue
            if isinstance(field, ForeignKeyFieldInstance) and len(field.source_fields) > 1:
                # A relation to a composite key is no column - its key columns are rendered below,
                # its constraint once per relation.
                continue
            if delete_field and field.model_field_name == delete_field.model_field_name:
                continue
            if field.model_field_name in delete_shadow_field_names:
                continue

            actual_field = field
            for old_f, new_f in alter_fields:
                if field.model_field_name == old_f.model_field_name:
                    actual_field = new_f
                    break

            if create_field and field.model_field_name == create_field.model_field_name:
                actual_field = create_field

            db_field = self._get_remake_column_name(actual_field)
            if db_field in fields_by_db_column:
                continue
            fields_by_db_column[db_field] = actual_field
        return fields_by_db_column

    def _is_deleted_constraint(self, constraint, delete_constraint) -> bool:
        """True if a Meta.constraints entry is the one being dropped by this rebuild.

        Matches by name when either side has one; a nameless UniqueConstraint falls back to a
        fields match.
        """
        if isinstance(delete_constraint, CheckConstraint):
            return isinstance(constraint, CheckConstraint) and constraint.name == delete_constraint.name
        if isinstance(constraint, UniqueConstraint):
            if delete_constraint.name or constraint.name:
                return constraint.name == delete_constraint.name
            return tuple(constraint.fields) == tuple(delete_constraint.fields)
        return False

    @staticmethod
    def _constraint_references_field(constraint, field) -> bool:
        """True if a Meta.constraints entry names ``field`` among its own fields - a
        UniqueConstraint by its fields, a CheckConstraint whose condition is a Q by the fields
        the condition reads. A CheckConstraint of raw SQL (``RawSQLTerm``) can't be read for field names."""
        if isinstance(constraint, CheckConstraint) and isinstance(constraint.check, Q):
            return field.model_field_name in constraint.check.get_referenced_field_names()
        return isinstance(constraint, UniqueConstraint) and field.model_field_name in constraint.fields

    @staticmethod
    def _index_references_field(index: Index, field: Field[Any]) -> bool:
        """True if a Meta.indexes entry keys on ``field`` - by its name, or for a relation by one of
        its key columns' fields. An expression index can't be read for field names.

        Args:
            index: The index.
            field: The field.

        Returns:
            Whether the index lists the field.
        """
        field_names = {field.model_field_name, *getattr(field, "source_fields", ())}
        return any(name.removeprefix("-") in field_names for name in index.fields)

    def _build_remake_field_definitions(
        self, model, fields_by_db_column, delete_field=None, alter_fields=None, delete_constraint=None
    ) -> list[str]:
        """Renders each surviving column's definition and the model's constraints - what the rebuilt
        table's ``CREATE TABLE`` holds.
        """
        db_table = model._meta.db_table
        qualified_table = self._qualify_table_name(db_table, model._meta.schema)
        field_definitions = []
        for db_field, actual_field in fields_by_db_column.items():
            # The column's comment is kept.
            comment_sql = (
                self._get_column_comment_sql(table=qualified_table, column=db_field, comment=actual_field.description)
                if actual_field.description
                else ""
            )
            if isinstance(actual_field, ForeignKeyFieldInstance):
                fk_field = actual_field
                field_type = fk_field.to_field_instance.get_column_type(self.client.dialect)

                if self.creates_foreign_key(fk_field):
                    to_field_name = (
                        fk_field.to_field_instance.source_field or fk_field.to_field_instance.model_field_name
                    )
                    field_def = self._get_field_sql(
                        db_field=db_field,
                        field_type=field_type,
                        nullable=actual_field.null,
                        unique=actual_field.unique and not actual_field.pk,
                        is_pk=actual_field.pk,
                        comment=comment_sql,
                    ) + self._get_fk_reference_string(
                        constraint_name=GeneratedNames.get_foreign_key_name(
                            db_table, (db_field,), fk_field.related_model._meta.db_table, (to_field_name,)
                        ),
                        db_field=db_field,
                        table=self._qualify_table_name(
                            fk_field.related_model._meta.db_table,
                            fk_field.related_model._meta.schema,
                        ),
                        field=to_field_name,
                        on_delete=fk_field.db_on_delete,
                        comment="",
                    )
                else:
                    # No database constraint (creates_foreign_key) means no FK constraint at
                    # all - a plain column.
                    field_def = self._get_field_sql(
                        db_field=db_field,
                        field_type=field_type,
                        nullable=actual_field.null,
                        unique=actual_field.unique and not actual_field.pk,
                        is_pk=actual_field.pk,
                        comment=comment_sql,
                    )
            elif actual_field.pk and actual_field.generated:
                generated_sql = actual_field.get_generated_sql(self.client.dialect)
                if generated_sql:
                    field_def = self.GENERATED_PK_TEMPLATE.format(
                        field_name=self.quote(db_field),
                        generated_sql=generated_sql,
                        comment=comment_sql,
                    )
                else:
                    field_def = self._get_field_sql(
                        db_field=db_field,
                        field_type=actual_field.get_column_type(self.client.dialect),
                        nullable=actual_field.null,
                        unique=False,
                        is_pk=True,
                        comment=comment_sql,
                    )
            else:
                generated_field_def = self._get_non_pk_generated_field_sql(actual_field, db_field, comment_sql)
                if generated_field_def:
                    field_def = generated_field_def
                else:
                    field_def = self._get_field_sql(
                        db_field=db_field,
                        field_type=actual_field.get_column_type(self.client.dialect),
                        nullable=actual_field.null,
                        unique=actual_field.unique and not actual_field.pk,
                        is_pk=actual_field.pk,
                        comment=comment_sql,
                    )

            if actual_field.has_db_default():
                if hasattr(actual_field.db_default, "get_sql"):
                    field_def += f" DEFAULT {actual_field.db_default.get_sql(self.client.dialect)}"
                else:
                    db_val = self.client.dialect.types.get_db_value(actual_field, actual_field.db_default, model)
                    escaped = self.client.dialect.get_literal_sql(db_val)
                    field_def += f" DEFAULT {escaped}"

            field_definitions.append(field_def)

        if model._meta.has_composite_primary_key:
            # A composite primary key's fields don't carry primary_key=True - its PRIMARY KEY
            # constraint is added here.
            composite_pk_columns = [
                model._meta.fields_map[name].source_field or name for name in model._meta.pk_attr_names
            ]
            field_definitions.append(self._get_composite_pk_constraint_sql(model, composite_pk_columns))

        # The check and unique constraints of Meta.constraints go into the CREATE TABLE.
        for constraint in getattr(model._meta, ModelOption.CONSTRAINTS, None) or ():
            if delete_constraint is not None and self._is_deleted_constraint(constraint, delete_constraint):
                continue
            # A constraint on the column this rebuild drops is dropped with it - `model` is rendered
            # from the old state and still has it.
            if delete_field is not None and self._constraint_references_field(constraint, delete_field):
                continue
            if isinstance(constraint, CheckConstraint):
                field_definitions.append(
                    self.CHECK_CONSTRAINT_CREATE_TEMPLATE.format(
                        name=self.quote(constraint.name),
                        check=ConstraintCondition.get_sql(constraint.check, model, self.client),
                    )
                )
            elif isinstance(constraint, UniqueConstraint):
                # A unique constraint is a unique index of its own name - _remake_table() creates
                # it once the table is in place, so it keeps its name and can be dropped without
                # another rebuild.
                self._check_unique_constraint_supported(constraint)

        # Composite relation constraints, one per relation: the altered field's new definition, and
        # none for the removed one.
        alter_by_name = {old.model_field_name: new for old, new in (alter_fields or ())}
        seen_composite_fields: set[str] = set()
        for field in model._meta.fields_map.values():
            if not isinstance(field, ForeignKeyFieldInstance) or len(field.source_fields) <= 1:
                continue
            if delete_field and field.model_field_name == delete_field.model_field_name:
                continue
            actual_field = alter_by_name.get(field.model_field_name, field)
            if actual_field.model_field_name in seen_composite_fields:
                continue
            seen_composite_fields.add(actual_field.model_field_name)
            if not self.creates_foreign_key(actual_field):
                continue
            constraint = self._get_composite_fk_constraint(model, actual_field)
            field_definitions.append(
                self.FOREIGN_KEY_CONSTRAINT_CREATE_TEMPLATE.format(
                    name=self.quote(constraint.name),
                    fields=", ".join(self.quote(f) for f in constraint.fields),
                    table=constraint.to_table,
                    to_fields=", ".join(self.quote(f) for f in constraint.to_fields),
                    on_delete=constraint.on_delete,
                )
            )

        return field_definitions

    @staticmethod
    def _get_added_column_names(create_field: Field[Any] | None) -> list[str]:
        """The columns a rebuild adding ``create_field`` creates.

        Args:
            create_field: The field being added, if any.

        Returns:
            Its key columns for a relation, its own column otherwise.
        """
        if create_field is None or isinstance(
            create_field, (ManyToManyFieldInstance, BackwardFKRelation, BackwardOneToOneRelation)
        ):
            return []
        if isinstance(create_field, ForeignKeyFieldInstance):
            return list(create_field.db_column_names)
        return [BaseSchemaEditor._get_remake_column_name(create_field)]

    def _get_remade_unique_constraints(
        self, model: type[Model], delete_field: Field[Any] | None, delete_constraint: Any
    ) -> list[UniqueConstraint]:
        """The unique constraints a table rebuild re-creates as unique indexes once the new table
        is in place.

        Args:
            model: The model the table is rebuilt from.
            delete_field: A field whose column the rebuild drops.
            delete_constraint: A constraint the rebuild drops.

        Returns:
            Every UniqueConstraint of the model the rebuild keeps.
        """
        return [
            constraint
            for constraint in getattr(model._meta, ModelOption.CONSTRAINTS, None) or ()
            if isinstance(constraint, UniqueConstraint)
            and not (delete_constraint is not None and self._is_deleted_constraint(constraint, delete_constraint))
            and not (delete_field is not None and self._constraint_references_field(constraint, delete_field))
        ]

    async def _get_index_names(self, table_name: str, schema: str | None) -> set[str] | None:
        """The names of a table's indexes, for a rebuild to re-create the ones it had.

        Args:
            table_name: The table.
            schema: Its schema.

        Returns:
            The names, None when the database can't list them - every index is re-created then.
        """
        return None

    async def _remake_table(
        self,
        model,
        create_field=None,
        delete_field=None,
        alter_fields=None,
        delete_constraint=None,
        added_column_name: str | None = None,
    ) -> None:
        """Rebuilds a table from the model: creates the new table under a temporary name, copies
        the rows into it, puts it in place of the old one, then re-creates its triggers, indexes
        and unique constraints, which went with the old table. The alteration strategy of a
        database whose ALTER TABLE can't make the change in place (SQLite's recommended one).

        Args:
            model: The model rendered from the state the table is rebuilt to.
            create_field: A field whose column the rebuild adds.
            delete_field: A field whose column the rebuild drops.
            alter_fields: (old, new) definitions of the fields the rebuild changes.
            delete_constraint: A constraint the rebuild drops.
            added_column_name: A column the running AddField added just before - like
                ``create_field``'s, its own index comes from the AddIndex that follows.
        """
        alter_fields = alter_fields or []
        db_table = model._meta.db_table
        new_table_name = f"new__{db_table}"

        column_mapping = self._build_remake_column_mapping(model, create_field, delete_field, alter_fields)
        fields_by_db_column = self._get_remake_fields_by_db_column(model, create_field, delete_field, alter_fields)
        field_definitions = self._build_remake_field_definitions(
            model, fields_by_db_column, delete_field, alter_fields, delete_constraint
        )

        qualified_new = self._qualify_table_name(new_table_name, model._meta.schema)
        qualified_old = self._qualify_table_name(db_table, model._meta.schema)
        # A field's own index is re-created only when the old table had it - one the running
        # migration removed comes back through the AddIndex that follows, if any.
        old_index_names = await self._get_index_names(db_table, model._meta.schema)
        table_options = model._meta.get_table_options(self.client.dialect)
        if table_options is not None:
            table_options.raise_if_unsupported(model)
        create_sql = (
            f"CREATE {table_options.get_create_prefix_sql() if table_options else ''}TABLE {qualified_new} "
            f"({', '.join(field_definitions)})"
            f"{table_options.get_create_suffix_sql(model, self.quote) if table_options else ''}"
        )
        await self._run_sql(create_sql)

        if column_mapping:
            columns = list(column_mapping.keys())
            values = list(column_mapping.values())
            insert_sql = f"""INSERT INTO {qualified_new} ({", ".join(self.quote(c) for c in columns)})
                SELECT {", ".join(values)}
                FROM {qualified_old}"""  # nosec B608
            await self._run_sql(insert_sql)

        await self._replace_table(db_table, new_table_name, model._meta.schema, fields_by_db_column.values())

        # Triggers went with the old table - created again.
        for trigger in model._meta.triggers:
            await self.add_trigger(model, trigger)

        # So did the indexes of Meta.indexes; an index on the dropped column isn't created again.
        normalized_meta_indexes = [
            index
            for index in (
                entry if isinstance(entry, Index) else Index(fields=tuple(entry)) for entry in model._meta.indexes
            )
            if delete_field is None or not self._index_references_field(index, delete_field)
        ]
        columns_covered_by_meta_indexes = set()
        column_lists_covered_by_meta_indexes = set()
        for index in normalized_meta_indexes:
            columns = self._column_names_for_index(model, index)
            column_lists_covered_by_meta_indexes.add(tuple(columns))
            if len(columns) == 1:
                columns_covered_by_meta_indexes.add(columns[0])

        # And the index of each surviving field's index=True. A column the running AddField added
        # gets its index from the AddIndex that follows.
        added_column_names = set(self._get_added_column_names(create_field))
        if added_column_name is not None:
            added_column_names.add(added_column_name)
        for db_field, actual_field in fields_by_db_column.items():
            field_index = Index(fields=(actual_field.model_field_name,))
            if (
                actual_field.index
                and not actual_field.pk
                and db_field not in columns_covered_by_meta_indexes
                and db_field not in added_column_names
                and (old_index_names is None or self._index_name_for_model(model, field_index) in old_index_names)
            ):
                await self.add_index(model, field_index)
        # A relation to a composite primary key has one index over all of its key columns.
        for relation_field_name, column_names in model._meta.get_field_index_columns():
            if (
                len(column_names) > 1
                and column_names not in column_lists_covered_by_meta_indexes
                and all(column_name in fields_by_db_column for column_name in column_names)
                and not added_column_names.intersection(column_names)
            ):
                await self.add_index(model, Index(fields=(relation_field_name,)))

        for index in normalized_meta_indexes:
            await self.add_index(model, index)

        # A unique constraint is a unique index of its own name, never part of the CREATE TABLE body.
        for constraint in self._get_remade_unique_constraints(model, delete_field, delete_constraint):
            await self.add_constraint(model, constraint)
