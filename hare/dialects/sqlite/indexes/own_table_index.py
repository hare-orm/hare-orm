from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from hare.ddl.indexes.index import Index
from hare.exceptions import ConfigurationError
from hare.fields.data.numeric.int_field import IntField
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.models import Model


class OwnTableIndex(Index):
    """An index of SQLite kept in a table of its own - FTS5's, SpatiaLite's R*Tree - and in step with
    the indexed table by triggers. Its table outlives a ``DROP TABLE`` of the indexed table and reads
    the table and its columns by name: the schema editor drops the index before the table is
    dropped, rebuilt or renamed or an indexed column renamed, and creates it again after.

    The index keys its rows by the model's integer primary key - a rowid SQLite never renumbers,
    which an implicit rowid isn't (``VACUUM`` may).
    """

    SUPPORTS_INCLUDE = False

    def get_table_name(self, model: type[Model], column_names: Sequence[str] | None = None) -> str:
        """The name of the index's table.

        Args:
            model: The indexed model.
            column_names: The indexed columns - the fields' current ones by default.

        Returns:
            The name.
        """
        if column_names is None:
            column_names = model._meta.get_column_names(self.fields)
        return self.get_index_table_name(model._meta.db_table, column_names)

    def get_index_table_name(self, table_name: str, column_names: Sequence[str]) -> str:
        """The name of the index's table over columns of a table.

        Args:
            table_name: The indexed table.
            column_names: The indexed columns.

        Returns:
            The name.
        """
        raise NotImplementedError

    def get_create_sqls(self, schema_editor: BaseSchemaEditor, model: type[Model], safe: bool) -> list[str]:
        """Returns the statements creating the index and filling it from the rows already there.

        Args:
            schema_editor: The schema editor of the database the DDL runs on.
            model: The indexed model.
            safe: Whether the index is created only when it doesn't exist yet.

        Returns:
            The statements.
        """
        raise NotImplementedError

    def get_drop_sqls(
        self, schema_editor: BaseSchemaEditor, table_name: str, column_names: Sequence[str]
    ) -> list[str]:
        """Returns the statements dropping the index, each part only when it exists.

        Args:
            schema_editor: The schema editor of the database the DDL runs on.
            table_name: The indexed table, as the database has it now.
            column_names: The indexed columns, as the database has them now.

        Returns:
            The statements.
        """
        raise NotImplementedError

    def get_sql(self, schema_editor: BaseSchemaEditor, model: type[Model], safe: bool) -> str:
        return "\n".join(self.get_create_sqls(schema_editor, model, safe))

    def get_row_key_column(self, model: type[Model]) -> str:
        """The column of the model's integer primary key, which keys the index's rows.

        Args:
            model: The indexed model.

        Returns:
            The column.

        Raises:
            ConfigurationError: The model's primary key isn't one integer column.
        """
        primary_key_attribute = model._meta.primary_key_attribute
        pk_field = (
            model._meta.fields_map.get(primary_key_attribute) if isinstance(primary_key_attribute, str) else None
        )
        key_field = pk_field.to_field_instance if isinstance(pk_field, ForeignKeyFieldInstance) else pk_field
        if not isinstance(key_field, IntField) or not isinstance(primary_key_attribute, str):
            raise ConfigurationError(
                f"{model.__name__}: {type(self).__name__} needs an integer primary key to key its rows by - "
                "SQLite may renumber the implicit rowid of a table without one"
            )
        return model._meta.get_column_names([primary_key_attribute])[0]
