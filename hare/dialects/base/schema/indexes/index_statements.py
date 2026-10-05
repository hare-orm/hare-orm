from __future__ import annotations

from collections.abc import Collection, Sequence
from copy import copy
from typing import Any

from hare.ddl.enums import GeneratedNamePrefix
from hare.ddl.generated_names import GeneratedNames
from hare.ddl.indexes.index import Index
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.fields.field import Field
from hare.models import Model


class IndexStatements(SchemaEditorPart):
    """The SQL of indexes: the CREATE and DROP INDEX statements, unique and partial indexes, the
    indexed fields and expressions, and the indexes reading columns that are renamed."""

    __slots__ = ()

    def format_index_fields(
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
        for position, field in enumerate(field_names):
            is_expression = field not in column_names and GeneratedNames.is_index_expression(field)
            column = field if is_expression else self.editor.quote(field)
            opclass = opclasses[position] if opclasses and position < len(opclasses) else ""
            # An opclass name is quoted like any identifier.
            order = orders[position] if orders and position < len(orders) else ""
            key = f"{column} {self.editor.quote(opclass)}" if opclass else column
            formatted.append(f"{key} {order}" if order else key)
        return ", ".join(formatted)

    def get_index_sql(
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
        """The ``CREATE INDEX`` of an index of a model's table.

        Args:
            model: The model.
            field_names: The indexed columns, or the SQL of each expression key.
            safe: Whether to add ``IF NOT EXISTS``.
            index_name: The index's name - generated from the columns when None.
            index_type: The access method (``USING ...``), empty for the default one.
            extra: SQL written after the keys - ``INCLUDE``, ``WITH``, the ``WHERE`` of a partial index.
            opclasses: The operator class of each key.
            unique: Whether the index is unique.
            orders: The sort order of each key.
            indexed_table_sql: The table indexed, when it isn't the model's own.

        Returns:
            The statement.
        """
        template = self.editor.UNIQUE_INDEX_CREATE_TEMPLATE if unique else self.editor.INDEX_CREATE_TEMPLATE
        prefix = GeneratedNamePrefix.UNIQUE_INDEX if unique else GeneratedNamePrefix.INDEX
        return template.format(
            exists=self.editor.table_creation.get_exists_sql(safe),
            index_name=self.editor.get_index_name_sql(
                index_name or GeneratedNames.get_index_name(prefix, model, field_names), model._meta.schema
            ),
            table_name=indexed_table_sql
            or self.editor.get_indexed_table_sql(model._meta.db_table, model._meta.schema),
            fields=self.format_index_fields(
                field_names, opclasses, column_names=set(model._meta.fields_db_projection.values()), orders=orders
            ),
            index_type=self.format_index_type(index_type) if index_type else "",
            extra=f"{extra}" if extra else "",
        )

    def format_index_type(self, index_type: str) -> str:
        """The index type as the dialect's CREATE INDEX statement writes it.

        Args:
            index_type: The index method (``Index.INDEX_TYPE``).

        Returns:
            The clause with its trailing space.
        """
        return f"{index_type} "

    def get_unique_index_sql(
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
        return self.editor.UNIQUE_INDEX_CREATE_TEMPLATE.format(
            exists=self.editor.table_creation.get_exists_sql(safe),
            index_name=self.editor.get_index_name_sql(index_name, schema),
            index_type="",
            table_name=self.editor.get_indexed_table_sql(table_name, schema),
            fields=", ".join([self.editor.quote(field_name) for field_name in field_names]),
            extra="",
        )

    def get_table_index_sql(
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
        return self.editor.INDEX_CREATE_TEMPLATE.format(
            exists=self.editor.table_creation.get_exists_sql(safe),
            index_name=self.editor.get_index_name_sql(
                GeneratedNames.get_index_name(GeneratedNamePrefix.INDEX, table_name, column_names), schema
            ),
            index_type="",
            table_name=self.editor.get_indexed_table_sql(table_name, schema),
            fields=", ".join([self.editor.quote(column_name) for column_name in column_names]),
            extra="",
        )

    def column_names_for_index(self, model: type[Model], index: Index) -> list[str]:
        """Returns the index's keys as this connection's ``CREATE INDEX`` lists them - a field's
        own column (its ``source_field``), an expression rendered by the connection's dialect.

        Args:
            model: The indexed model.
            index: The index.

        Returns:
            One key each.
        """
        return index.get_key_sqls(model, self.editor.client.dialect)

    @staticmethod
    def get_field_indexes(model: type[Model], field: Field[Any]) -> list[Index]:
        """The indexes a field's own ``db_index=True`` implies - its column's, or one over all the key
        columns of a relation to a composite key - as the model lists them (``get_field_index_columns()``).

        Args:
            model: The model.
            field: One of its fields.

        Returns:
            The indexes, unnamed.
        """
        names = {field.model_field_name, *getattr(field, "source_fields", ())}
        return [
            Index(fields=(field_name,))
            for field_name, _column_names in model._meta.get_field_index_columns()
            if field_name in names
        ]

    def get_indexes_covering_field(
        self, model: type[Model], field: Field[Any], field_is_indexed: bool, *, adds_field_index: bool = False
    ) -> list[Index]:
        """The indexes covering a field's column - its own index and the ``Meta.indexes`` entries keying on
        it.

        Args:
            model: The model.
            field: The field.
            field_is_indexed: Whether the field has an index of its own.
            adds_field_index: Whether the change ``model`` is after adds the field's own index - it isn't
                there yet, though ``model`` lists it.

        Returns:
            The indexes.
        """
        # Local import: the table rebuild part reads this module's statements.
        from hare.dialects.base.schema.tables.table_rebuild import TableRebuild

        column = field.source_field or field.model_field_name
        field_index = Index(fields=(column,))
        indexes = [field_index] if field_is_indexed else []
        # The field's own index may be listed in Meta.indexes too - held once, and not at all while the
        # change only adds it.
        names = {self.index_name_for_model(model, field_index)} if field_is_indexed or adds_field_index else set()
        for entry in model._meta.indexes or ():
            index = entry if isinstance(entry, Index) else Index(fields=tuple(entry))
            # The field's own index may be listed there too.
            if (
                TableRebuild.index_references_field(index, field)
                and (name := self.index_name_for_model(model, index)) not in names
            ):
                names.add(name)
                indexes.append(index)
        return indexes

    def get_declared_index_names(self, model: type[Model]) -> list[str]:
        """The name of each index the model declares - its fields' own indexes, then ``Meta.indexes``. A
        name listed twice is one index the database holds once: a field's own index declared in
        ``Meta.indexes`` too.

        Args:
            model: The model.

        Returns:
            The names, a repeated one as often as it is declared.
        """
        names = [
            GeneratedNames.get_index_name(GeneratedNamePrefix.INDEX, model, column_names)
            for _, column_names in model._meta.get_field_index_columns()
        ]
        names.extend(
            self.index_name_for_model(model, entry if isinstance(entry, Index) else Index(fields=tuple(entry)))
            for entry in model._meta.indexes or ()
        )
        return names

    def is_declared_by_another_entry(self, model: type[Model], index: Index, *, model_declares_index: bool) -> bool:
        """Whether another declaration of the model names an index as this one is named - a field's own
        index listed in ``Meta.indexes`` too: the database holds it once, so it is created by the first
        declaration and dropped with the last.

        Args:
            model: The model.
            index: The index.
            model_declares_index: Whether ``model`` itself lists the index.

        Returns:
            Whether another declaration names it.
        """
        name = self.index_name_for_model(model, index)
        return self.get_declared_index_names(model).count(name) > int(model_declares_index)

    def index_name_for_model(
        self, model: type[Model], index: Index, renamed_column: tuple[str, str] | None = None
    ) -> str:
        """The index's name - its own, else generated from the table and its keys.

        Args:
            model: The indexed model.
            index: The index.
            renamed_column: ``(old column, new column)`` - the generated name the index got before
                the column was renamed, the new column taking the old one's place among the keys.

        Returns:
            The name.
        """
        if index.name:
            return index.name
        index.get_expressions(model)
        # Named after the keys in plain SQL - the same name on every database.
        column_names = (
            self.editor.constraint_names.get_fields_to_columns(model, index.field_names)
            if index.fields
            else list(index.field_names)
        )
        if renamed_column is not None:
            old_column, new_column = renamed_column
            column_names = [old_column if column_name == new_column else column_name for column_name in column_names]
        return GeneratedNames.get_index_name(
            GeneratedNamePrefix.UNIQUE_INDEX if index.unique else GeneratedNamePrefix.INDEX,
            model,
            column_names,
            index.get_name_parts(),
        )

    def get_index_before_column_rename(
        self, model: type[Model], index: Index, old_column: str, new_column: str
    ) -> Index:
        """The index under the name it has before its column ``new_column`` was renamed from
        ``old_column`` - a generated name is made from the columns.

        Args:
            model: The indexed model, its column already renamed.
            index: The index.
            old_column: The column's old name.
            new_column: Its new name.

        Returns:
            A copy carrying the old name - the index itself when it is named or the column kept its name.
        """
        if index.name or old_column == new_column:
            return index
        named_index = copy(index)
        named_index.name = self.index_name_for_model(model, index, (old_column, new_column))
        return named_index

    def get_index_create_sql(
        self,
        model: type[Model],
        index: Index,
        index_name: str | None = None,
        indexed_table_sql: str | None = None,
        safe: bool = False,
    ) -> str:
        """Returns the statement creating an index - with its table, or added to it later.

        Args:
            model: The indexed model.
            index: The index.
            index_name: The index's name, its own by default.
            indexed_table_sql: The table the index is created on, the model's by default.
            safe: Whether the index is created only when it doesn't exist yet.

        Returns:
            The statement.
        """
        index.get_expressions(model)
        return self.get_index_sql(
            model,
            self.column_names_for_index(model, index),
            safe,
            index_name=index_name or self.index_name_for_model(model, index),
            index_type=index.get_index_type_sql(),
            extra=index.get_extra(model, self.editor.client),
            opclasses=index.opclasses or None,
            unique=index.unique,
            orders=index.get_column_orders(model) or None,
            indexed_table_sql=indexed_table_sql,
        )

    def get_index_drop_sql(self, model: type[Model], index: Index) -> str:
        """Returns the statement dropping an index.

        Args:
            model: The indexed model.
            index: The index.

        Returns:
            The statement.
        """
        index.raise_if_not_droppable(self.editor.client.features, self.editor.client.dialect)
        index_name = self.index_name_for_model(model, index)
        return self.editor.DROP_INDEX_TEMPLATE.format(
            name=self.editor.qualify_table_name(index_name, model._meta.schema),
            table=self.editor.qualify_table_name(model._meta.db_table, model._meta.schema),
        )

    async def drop_indexes_reading_columns_to_rename(self, old_model: type[Model], new_model: type[Model]) -> None:
        """Drops the indexes that read their table's columns by name, before a column of the table
        is renamed - none by default: an index follows its column.

        Args:
            old_model: The model before the rename.
            new_model: The model after it - its column not renamed yet.
        """

    async def recreate_indexes_reading_renamed_columns(self, old_model: type[Model], new_model: type[Model]) -> None:
        """Creates again the indexes ``drop_indexes_reading_columns_to_rename()`` dropped, after a
        column of the table was renamed - none by default.

        Args:
            old_model: The model before the rename.
            new_model: The model after it - its column already renamed.
        """

    @classmethod
    def get_index_include_sql(cls, quoted_columns: Sequence[str]) -> str:
        """The clause storing non-key columns in an index, after its keys.

        Args:
            quoted_columns: The non-key columns, quoted.

        Returns:
            The clause with its leading space; ``""`` by default - a dialect without non-key index
            columns leaves them out, as they only make the index cover more queries.
        """
        return ""
