from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from hare.ddl.constraints.foreign_key_constraint import ForeignKeyConstraint
from hare.ddl.generated_names import GeneratedNames
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.exceptions import UnSupportedError
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.models import Model


class ColumnDefinitions(SchemaEditorPart):
    """The SQL of a model's columns: each field's column type, default and generated expression, the
    column definitions of a CREATE TABLE, and the reference a foreign key column makes."""

    __slots__ = ()

    def get_db_default_sql(self, db_default: Any) -> str:
        """The SQL of a ``db_default`` expression, checked against the connected server.

        Args:
            db_default: The ``SqlDefault``.

        Returns:
            The SQL.

        Raises:
            UnSupportedError: The default needs a feature the server lacks.
        """
        required_feature = getattr(db_default, "required_feature", None)
        if required_feature is not None and not getattr(self.editor.client.features, required_feature):
            raise UnSupportedError(f"{db_default!r} needs a newer {self.editor.client.dialect} server")
        return cast("str", db_default.get_sql(self.editor.client.dialect))

    def get_generated_column_sql(self, field: Field[Any]) -> str | None:
        """The generation clause of a generated column, checked against the connected server.

        Args:
            field: The generated field.

        Returns:
            The clause, None when the field has none.

        Raises:
            UnSupportedError: The column is computed on read and the server has no such columns.
        """
        if (
            getattr(field, "stored", True) is False
            and not self.editor.client.features.supports_virtual_generated_columns
        ):
            raise UnSupportedError(
                f"GeneratedField '{field.model_field_name}' has stored=False, but the {self.editor.client.dialect} "
                "server only supports STORED generated columns."
            )
        return field.get_generated_column_sql(self.editor.client.dialect)

    def get_field_sql(
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
                constraints (``Features.supports_unique_constraints``).
            is_pk: Whether the column is the table's single-column primary key.
            comment: The inline column comment, used where the connection supports inline comments.
            default: The ``DEFAULT`` clause, with its leading space, or an empty string.

        Returns:
            The column definition.
        """
        return self.editor.FIELD_TEMPLATE.format(
            name=self.editor.quote(db_field),
            type=field_type,
            nullable="" if nullable else " NOT NULL",
            unique=" UNIQUE"
            if unique and not is_pk and self.editor.client.features.supports_unique_constraints
            else "",
            primary=" PRIMARY KEY" if is_pk else "",
            default=default,
            comment=comment if self.editor.client.features.inline_comments else "",
        ).strip()

    def get_altered_column_type(self, sql_type: str, nullable: bool) -> str:
        """The type a column is changed to - the given ``{sql_type}`` of the ALTER templates.

        Args:
            sql_type: The field's column type.
            nullable: Whether the column accepts NULL.

        Returns:
            The type - the field's own; a database whose type says whether it holds NULL adds that.
        """
        return sql_type

    def get_column_trailing_sql(self, model: type[Model], field_name: str, description: str | None) -> str:
        """What follows a column's type and default - its inline comment, then the clauses the
        table's ``Meta.table_options`` of the dialect give the column.

        Args:
            model: The model whose table holds the column.
            field_name: The column's field.
            description: The column's comment, None or empty for none.

        Returns:
            The SQL, each part with its leading space; empty for neither.
        """
        meta = model._meta
        trailing_sql = (
            self.editor.table_comments.get_column_comment_sql(
                table=self.editor.qualify_table_name(meta.db_table, meta.schema),
                column=meta.fields_db_projection[field_name],
                comment=description,
            )
            if description
            else ""
        )
        table_options = meta.get_table_options(self.editor.client.dialect)
        return (
            trailing_sql if table_options is None else trailing_sql + table_options.get_column_clauses_sql(field_name)
        )

    def get_table_column_type(self, model: type[Model], field: Field[Any]) -> str:
        """Returns the type a column of a model's table is declared with - the dialect's type of the
        field, as the table's ``Meta.table_options`` of the dialect declare it.

        Args:
            model: The model whose table holds the column.
            field: The field whose type the column has.

        Returns:
            The column type.
        """
        column_type = field.get_column_type(self.editor.client.dialect)
        table_options = model._meta.get_table_options(self.editor.client.dialect)
        return column_type if table_options is None else table_options.get_column_type(column_type)

    def get_non_pk_generated_field_sql(
        self, model: type[Model], field_object: Field[Any], db_field: str, comment: str = ""
    ) -> str | None:
        """Non-PK `GENERATED ALWAYS AS (...)` column definition, or None if not applicable.

        Args:
            model: The model whose table holds the column.
            field_object: field to render.
            db_field: quoted column name source.
            comment: inline column comment SQL.
        Returns:
            The column definition string, or None when the field isn't a non-PK generated field
            or the dialect has no GENERATED_SQL for it.
        """
        if not (field_object.generated and not field_object.pk):
            return None
        generated_sql = self.get_generated_column_sql(field_object)
        if not generated_sql:
            return None
        return self.get_field_sql(
            db_field=db_field,
            field_type=f"{self.get_table_column_type(model, field_object)} {generated_sql}",
            nullable=field_object.null,
            unique=field_object.unique,
            is_pk=False,
            comment=comment,
        )

    def get_foreign_key_reference_string(
        self,
        constraint_name: str,
        db_field: str,
        table: str,
        field: str,
        on_delete: str,
        comment: str,
    ) -> str:
        return self.editor.FOREIGN_KEY_TEMPLATE.format(
            db_field=db_field,
            table=table,
            field=self.editor.quote(field),
            on_delete=on_delete,
            comment=comment,
        )

    def get_column_default_sql(self, field_object: Field[Any], model: type[Model]) -> str:
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
            return f" DEFAULT {self.get_db_default_sql(db_default)}"
        db_value = self.editor.client.dialect.types.get_db_value(field_object, db_default, model)
        return f" DEFAULT {self.editor.client.dialect.literals.get_literal_sql(db_value)}"

    def get_column_definitions(
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
        composite_foreign_key_constraints: list[ForeignKeyConstraint] = []
        for field_name, db_field in model._meta.fields_db_projection.items():
            field_object = model._meta.fields_map[field_name]
            comment = self.get_column_trailing_sql(model, field_name, field_object.description)
            if field_object.pk and field_object.generated:
                generated_sql = field_object.get_generated_sql(self.editor.client.dialect)
                if generated_sql:
                    column_definitions.append(
                        self.editor.GENERATED_PK_TEMPLATE.format(
                            field_name=self.editor.quote(db_field), generated_sql=generated_sql, comment=comment
                        )
                    )
                    continue
            if field_object.generated and not field_object.pk:
                generated_column_sql = self.get_non_pk_generated_field_sql(model, field_object, db_field, comment)
                if generated_column_sql:
                    column_definitions.append(generated_column_sql)
                    continue
            default = self.get_column_default_sql(field_object, model)
            reference = cast("ForeignKeyFieldInstance[Model] | None", getattr(field_object, "reference", None))
            if reference:
                column_definitions.append(self.get_foreign_key_field_definition(model, field_name, default))
                if self.editor.foreign_key_rebuild.creates_foreign_key(reference):
                    # Only a real constraint orders the tables: a relation without one has
                    # no DDL-level dependency at all, and must never make a model-level cycle look
                    # like a cyclic reference between tables.
                    related_meta = reference.related_model._meta
                    references.add((related_meta.schema, related_meta.db_table))
                    if len(reference.to_field_instances) > 1 and db_field == reference.source_fields[0]:
                        composite_foreign_key_constraints.append(
                            self.editor.table_creation.get_composite_foreign_key_constraint(model, reference)
                        )
                continue
            column_definitions.append(
                self.get_field_sql(
                    db_field=db_field,
                    field_type=self.get_table_column_type(model, field_object),
                    nullable=field_object.null,
                    unique=field_object.unique,
                    is_pk=field_object.pk,
                    comment=comment,
                    default=default,
                )
            )
        return column_definitions, references, composite_foreign_key_constraints

    def get_pk_column_type(self, pk_field: Field[Any]) -> str:
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
            return self.get_pk_column_type(pk_field.related_model._meta.pk)
        if sql_type := pk_field.get_column_type(self.editor.client.dialect):
            return sql_type
        raise UnSupportedError(f"Can't get SQL type of {pk_field} for {self.editor.client.dialect.name}")

    def get_foreign_key_field_definition(
        self, model: type[Model], key_field_name: str, default: str = "", *, with_reference: bool = True
    ) -> str:
        """Returns the column definition of a relation's key column.

        A single-column key with a database constraint carries an inline ``REFERENCES`` clause; a
        composite key's columns are plain, their table-level constraint added separately
        (``TableCreation.get_composite_foreign_key_constraint()``); a relation without a database constraint
        (``creates_foreign_key``) gives a plain column.

        Args:
            model: The model declaring the relation.
            key_field_name: The key field's name.
            default: The column's ``DEFAULT`` clause, with its leading space, or an empty string.
            with_reference: False leaves the ``REFERENCES`` clause out - the constraint is added
                on its own.

        Returns:
            The column definition.
        """
        key_field = model._meta.fields_map[key_field_name]
        foreign_key_field = cast("ForeignKeyFieldInstance[Model]", key_field.reference)
        db_field = model._meta.fields_db_projection[key_field_name]
        comment = self.get_column_trailing_sql(model, key_field_name, foreign_key_field.description)
        column_type = self.get_table_column_type(model, key_field)
        if (
            len(foreign_key_field.to_field_instances) > 1
            or not self.editor.foreign_key_rebuild.creates_foreign_key(foreign_key_field)
            or not with_reference
        ):
            return self.get_field_sql(
                db_field=db_field,
                field_type=column_type,
                nullable=key_field.null,
                unique=key_field.unique and len(foreign_key_field.to_field_instances) == 1,
                is_pk=key_field.pk,
                comment=comment,
                default=default,
            )

        to_field_name = (
            foreign_key_field.to_field_instance.source_field or foreign_key_field.to_field_instance.model_field_name
        )
        related_model = foreign_key_field.related_model
        return self.get_field_sql(
            db_field=db_field,
            field_type=column_type,
            nullable=key_field.null,
            unique=key_field.unique,
            is_pk=key_field.pk,
            comment="",
            default=default,
        ) + self.get_foreign_key_reference_string(
            constraint_name=GeneratedNames.get_foreign_key_name(
                model._meta.db_table, (db_field,), related_model._meta.db_table, (to_field_name,)
            ),
            db_field=db_field,
            table=self.editor.qualify_table_name(related_model._meta.db_table, related_model._meta.schema),
            field=to_field_name,
            on_delete=foreign_key_field.db_on_delete,
            comment=comment,
        )

    def get_key_columns_sql(self, column_names: Sequence[str], without_overlaps: bool) -> str:
        """The columns of a key, the last one compared ``WITHOUT OVERLAPS`` when asked.

        Args:
            column_names: The columns.
            without_overlaps: Whether the last column is a range the rows may not overlap in.

        Returns:
            The comma-separated quoted columns.
        """
        quoted_columns = [self.editor.quote(column_name) for column_name in column_names]
        if without_overlaps:
            quoted_columns[-1] += " WITHOUT OVERLAPS"
        return ", ".join(quoted_columns)
