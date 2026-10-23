from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from hare.ddl.constraints.foreign_key_constraint import ForeignKeyConstraint
from hare.ddl.enums import GeneratedNamePrefix
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

    def get_field_db_default_sql(self, field: Field[Any], model: type[Model]) -> str:
        """The SQL of a field's ``db_default`` - an expression's own, a value as its literal.

        Args:
            field: The field, which has a ``db_default``.
            model: Its model.

        Returns:
            The SQL.
        """
        if hasattr(field.db_default, "get_sql"):
            return self.get_db_default_sql(field.db_default)
        dialect = self.editor.client.dialect
        return dialect.literals.get_literal_sql(dialect.types.get_db_value(field, field.db_default, model))

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
                f"GeneratedField {field.model_field_name!r} has stored=False, but the {self.editor.client.dialect} "
                "server only supports STORED generated columns."
            )
        return field.get_generated_column_sql(self.editor.client.dialect)

    def get_field_sql(
        self,
        db_field: str,
        field_type: str,
        nullable: bool,
        primary_key_name: str | None,
        comment: str,
        default: str = "",
    ) -> str:
        """Returns one column's definition inside ``CREATE TABLE``/``ADD COLUMN`` - its uniqueness is a
        named constraint of its own, made apart from the column.

        Args:
            db_field: The column's name.
            field_type: The column's SQL type, with any clause that follows it
                (``GENERATED ALWAYS AS (...)``).
            nullable: Whether the column accepts NULL.
            primary_key_name: The name of the table's single-column primary key the column is, None
                for another column.
            comment: The inline column comment, used where the connection supports inline comments.
            default: The ``DEFAULT`` clause, with its leading space, or an empty string.

        Returns:
            The column definition.
        """
        return self.editor.FIELD_TEMPLATE.format(
            name=self.editor.quote(db_field),
            type=field_type,
            nullable="" if nullable else " NOT NULL",
            primary=self.get_primary_key_clause(primary_key_name) if primary_key_name is not None else "",
            default=default,
            comment=comment if self.editor.client.features.inline_comments else "",
        ).strip()

    def get_primary_key_clause(self, name: str) -> str:
        """The clause making a column the table's primary key, under its name.

        Args:
            name: The primary key's name.

        Returns:
            The clause with its leading space.
        """
        return f" CONSTRAINT {self.editor.quote(name)} PRIMARY KEY"

    @staticmethod
    def get_primary_key_name(model: type[Model], column_names: Sequence[str]) -> str:
        """The name of a model's primary key - made from its table and columns.

        Args:
            model: The model.
            column_names: The key's columns.

        Returns:
            The name.
        """
        return GeneratedNames.get_index_name(GeneratedNamePrefix.PRIMARY_KEY, model, list(column_names))

    def get_column_definition(
        self, model: type[Model], field_name: str, *, with_reference: bool = True, nullable: bool | None = None
    ) -> str:
        """The definition of one column of a model's table - the only place one is made: a generated
        primary key, a generated column, a relation's key column (with its foreign key), a plain column.

        Args:
            model: The model.
            field_name: The field holding the column.
            with_reference: False leaves a key column's foreign key out - it is added on its own.
            nullable: Whether the column accepts NULL - the field's own setting when None.

        Returns:
            The definition.
        """
        meta = model._meta
        db_field = meta.fields_db_projection[field_name]
        field_object = meta.fields_map[field_name]
        comment = self.get_column_trailing_sql(model, field_name, self.get_column_description(model, field_name))
        if field_object.pk and field_object.generated:
            generated_sql = field_object.get_generated_sql(self.editor.client.dialect)
            if generated_sql:
                return self.editor.GENERATED_PK_TEMPLATE.format(
                    field_name=self.editor.quote(db_field),
                    generated_sql=generated_sql,
                    primary_key=self.get_primary_key_clause(self.get_primary_key_name(model, (db_field,))),
                    comment=comment,
                )
        if field_object.generated and not field_object.pk:
            generated_column_sql = self.get_non_pk_generated_field_sql(model, field_object, db_field, comment)
            if generated_column_sql:
                return generated_column_sql
        default = self.get_column_default_sql(field_object, model)
        if getattr(field_object, "reference", None):
            return self.get_foreign_key_field_definition(model, field_name, default, with_reference=with_reference)
        return self.get_field_sql(
            db_field=db_field,
            field_type=self.get_table_column_type(model, field_object),
            nullable=field_object.null if nullable is None else nullable,
            primary_key_name=self.get_primary_key_name(model, (db_field,)) if field_object.pk else None,
            comment=comment,
            default=default,
        )

    def get_altered_column_type(self, sql_type: str, nullable: bool) -> str:
        """The type a column is changed to - the given ``{sql_type}`` of the ALTER templates.

        Args:
            sql_type: The field's column type.
            nullable: Whether the column accepts NULL.

        Returns:
            The type - the field's own; a database whose type says whether it holds NULL adds that.
        """
        return sql_type

    @staticmethod
    def get_column_description(model: type[Model], field_name: str) -> str | None:
        """A column's comment - a relation's key column takes the relation's description.

        Args:
            model: The model.
            field_name: The column's field.

        Returns:
            The description, None for none.
        """
        field = model._meta.fields_map[field_name]
        reference = getattr(field, "reference", None)
        return reference.description if reference is not None else field.description

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
            trailing_sql
            if table_options is None
            else trailing_sql + table_options.get_column_clauses_sql(model, field_name, self.editor)
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
            primary_key_name=None,
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
        """The definition of a foreign key column's reference - ``REFERENCES table (field) ON DELETE ...``.

        Args:
            constraint_name: The constraint's name.
            db_field: The column.
            table: The table referenced.
            field: The column referenced.
            on_delete: The ``ON DELETE`` action.
            comment: The column's comment SQL.

        Returns:
            The SQL.
        """
        return self.editor.FOREIGN_KEY_TEMPLATE.format(
            name=self.editor.quote(constraint_name),
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
        self, model: type[Model], deferred_table_keys: frozenset[tuple[str | None, str]] = frozenset()
    ) -> tuple[list[str], set[tuple[str | None, str]], list[ForeignKeyConstraint], list[ForeignKeyConstraint]]:
        """Returns the column definitions of a model's table.

        Args:
            model: The model.
            deferred_table_keys: The ``(schema, table)`` of the tables created after this one - a
                foreign key to one of them is added once they exist.

        Returns:
            The column definitions in field order; the ``(schema, table)`` of every table a real
            foreign key constraint points at; the table-level constraints of composite keys; and the
            foreign keys to the tables of ``deferred_table_keys``.
        """
        column_definitions: list[str] = []
        references: set[tuple[str | None, str]] = set()
        composite_foreign_key_constraints: list[ForeignKeyConstraint] = []
        deferred_foreign_key_constraints: list[ForeignKeyConstraint] = []
        for field_name, db_field in model._meta.fields_db_projection.items():
            reference = cast(
                "ForeignKeyFieldInstance[Model] | None", getattr(model._meta.fields_map[field_name], "reference", None)
            )
            if not reference:
                column_definitions.append(self.get_column_definition(model, field_name))
                continue
            related_meta = reference.related_model._meta
            creates_foreign_key = self.editor.foreign_key_rebuild.creates_foreign_key(reference)
            is_deferred = creates_foreign_key and (related_meta.schema, related_meta.db_table) in deferred_table_keys
            column_definitions.append(self.get_column_definition(model, field_name, with_reference=not is_deferred))
            if not creates_foreign_key:
                continue
            is_composite = len(reference.to_field_instances) > 1
            if is_deferred:
                if not is_composite:
                    deferred_foreign_key_constraints.append(
                        self.editor.table_creation.get_single_column_foreign_key_constraint(model, field_name)
                    )
                elif db_field == reference.source_fields[0]:
                    deferred_foreign_key_constraints.append(
                        self.editor.table_creation.get_composite_foreign_key_constraint(model, reference)
                    )
                continue
            # Only a real constraint orders the tables: a relation without one has no DDL-level
            # dependency at all, and must never make a model-level cycle look like a cyclic reference
            # between tables.
            references.add((related_meta.schema, related_meta.db_table))
            if is_composite and db_field == reference.source_fields[0]:
                composite_foreign_key_constraints.append(
                    self.editor.table_creation.get_composite_foreign_key_constraint(model, reference)
                )
        return column_definitions, references, composite_foreign_key_constraints, deferred_foreign_key_constraints

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
        comment = self.get_column_trailing_sql(
            model, key_field_name, self.get_column_description(model, key_field_name)
        )
        column_type = self.get_table_column_type(model, key_field)
        primary_key_name = self.get_primary_key_name(model, (db_field,)) if key_field.pk else None
        if (
            len(foreign_key_field.to_field_instances) > 1
            or not self.editor.foreign_key_rebuild.creates_foreign_key(foreign_key_field)
            or not with_reference
        ):
            return self.get_field_sql(
                db_field=db_field,
                field_type=column_type,
                nullable=key_field.null,
                primary_key_name=primary_key_name,
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
            primary_key_name=primary_key_name,
            comment="",
            default=default,
        ) + self.get_foreign_key_reference_string(
            constraint_name=GeneratedNames.get_foreign_key_name(model._meta.db_table, (db_field,)),
            db_field=db_field,
            table=self.editor.get_referenced_table_sql(related_model._meta.db_table, related_model._meta.schema),
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
