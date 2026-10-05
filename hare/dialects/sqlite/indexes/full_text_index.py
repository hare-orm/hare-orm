from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from hare.ddl.enums import GeneratedNamePrefix
from hare.ddl.generated_names import GeneratedNames
from hare.dialects.sqlite.constants import (
    SQLITE_FULL_TEXT_CONTENT_OPTION,
    SQLITE_FULL_TEXT_CONTENT_ROWID_OPTION,
    SQLITE_FULL_TEXT_DELETE_TRIGGER_SUFFIX,
    SQLITE_FULL_TEXT_INSERT_TRIGGER_SUFFIX,
    SQLITE_FULL_TEXT_MODULE,
    SQLITE_FULL_TEXT_TOKENIZE_OPTION,
    SQLITE_FULL_TEXT_UPDATE_TRIGGER_SUFFIX,
)
from hare.dialects.sqlite.indexes.constants import (
    SQLITE_FULL_TEXT_DELETE_COMMAND,
    SQLITE_FULL_TEXT_MAX_TOKENIZER_LENGTH,
    SQLITE_FULL_TEXT_REBUILD_COMMAND,
    SQLITE_FULL_TEXT_TOKENIZER_DESCRIPTION,
)
from hare.dialects.sqlite.indexes.own_table_index import OwnTableIndex
from hare.exceptions import ConfigurationError, UnSupportedError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.features import Features
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.models import Model


class FullTextIndex(OwnTableIndex):
    """A full-text index of a model's text fields on SQLite - an FTS5 table reading its text from
    the model's table (``content=``), kept in step with it by triggers on insert, update and
    delete, and filled from the rows already there when it is created. ``field__search=`` matches
    through it; ``SearchRank`` and ``SearchHeadline`` read its ranking and highlighting.

    Args:
        fields: The indexed fields.
        name: The FTS5 table's name - generated from the table and fields by default.
        tokenizer: FTS5's ``tokenize`` option (``"porter unicode61"``, ``"trigram"``) - FTS5's
            own default (``unicode61``) when None.

    Raises:
        ConfigurationError: No fields, a descending key, or a tokenizer that isn't non-empty text.
    """

    INDEX_TYPE = "FTS5"
    TEXT_STORAGE_PARAMETERS = ("tokenizer",)

    def __init__(self, *, fields: Sequence[str], name: str | None = None, tokenizer: str | None = None) -> None:
        if isinstance(fields, str) or not fields:
            raise ConfigurationError(f"FullTextIndex fields must be a non-empty list of field names, got {fields!r}")
        super().__init__(fields=tuple(fields), name=name)
        if any(self.field_orders):
            raise ConfigurationError("FullTextIndex has no key order - its fields are named without '-'")
        if tokenizer is not None and (
            not isinstance(tokenizer, str)
            or not tokenizer.strip()
            or len(tokenizer) > SQLITE_FULL_TEXT_MAX_TOKENIZER_LENGTH
            or "\x00" in tokenizer
        ):
            raise ConfigurationError(
                "FullTextIndex tokenizer must be non-empty text of at most "
                f"{SQLITE_FULL_TEXT_MAX_TOKENIZER_LENGTH} characters, got {tokenizer!r}"
            )
        self.tokenizer = tokenizer
        # Never written into SQL - it sets the index apart from another one over the same fields,
        # in its generated name and in the migrations' comparison of indexes.
        self.extra = SQLITE_FULL_TEXT_TOKENIZER_DESCRIPTION.format(tokenizer=tokenizer) if tokenizer else ""

    @classmethod
    def get_covering(cls, model: type[Model], field_names: Iterable[str]) -> FullTextIndex | None:
        """The model's first full-text index over every one of the fields.

        Args:
            model: The model.
            field_names: The fields.

        Returns:
            The index, None when no full-text index covers them.
        """
        wanted_field_names = set(field_names)
        for index in model._meta.indexes:
            if isinstance(index, cls) and wanted_field_names <= set(index.fields):
                return index
        return None

    def raise_if_unsupported(self, dialect: Dialect) -> None:
        """Rejects the index on a dialect without full-text indexes.

        Raises:
            UnSupportedError: The dialect has no ``features.supports_full_text_index``.
        """
        self.raise_if_missing_feature(dialect.features, str(dialect))

    def raise_if_missing_feature(self, features: Features, database_name: str) -> None:
        """Rejects the index where the features have no full-text indexes.

        Args:
            features: The features of the dialect or the connection.
            database_name: What the features are of, for the message.

        Raises:
            UnSupportedError: No ``features.supports_full_text_index``.
        """
        if not features.supports_full_text_index:
            raise UnSupportedError(
                f"FullTextIndex(fields={list(self.fields)!r}) can't be created on {database_name}: it needs "
                "features.supports_full_text_index - SQLite's FTS5"
            )

    def raise_if_not_droppable(self, features: Features, dialect: Dialect) -> None:
        """Rejects dropping the index where it can't exist.

        Raises:
            UnSupportedError: The connection has no full-text indexes.
        """
        self.raise_if_missing_feature(features, "this connection")

    def get_index_table_name(self, table_name: str, column_names: Sequence[str]) -> str:
        """The name of the index's FTS5 table - its own name, or the one generated for the index."""
        if self.name:
            return self.name
        return GeneratedNames.get_index_name(
            GeneratedNamePrefix.INDEX, table_name, column_names, self.get_name_parts()
        )

    def get_extra(self, model: type[Model], client: DatabaseClient) -> str:
        """Rejects the index where it can't be created - a plain ``CREATE INDEX`` is never written
        for it.

        Raises:
            UnSupportedError: The connection has no full-text indexes.
        """
        self.raise_if_missing_feature(client.features, repr(client.connection_alias))
        return ""

    def get_create_sqls(self, schema_editor: BaseSchemaEditor, model: type[Model], safe: bool) -> list[str]:
        """Returns the statements creating the FTS5 table, its triggers, and filling it.

        Raises:
            UnSupportedError: The connection has no full-text indexes.
            ConfigurationError: The model has no integer primary key.
        """
        client = schema_editor.client
        self.raise_if_missing_feature(client.features, repr(client.connection_alias))
        quote = schema_editor.quote
        literals = client.dialect.literals
        exists = "IF NOT EXISTS " if safe else ""
        table_name = self.get_table_name(model)
        indexed_table = quote(model._meta.db_table)
        index_table = quote(table_name)
        row_key_column = self.get_row_key_column(model)
        columns = [quote(column) for column in model._meta.get_column_names(self.fields)]
        options = [
            f"{SQLITE_FULL_TEXT_CONTENT_OPTION}={literals.get_string_literal_sql(model._meta.db_table)}",
            f"{SQLITE_FULL_TEXT_CONTENT_ROWID_OPTION}={literals.get_string_literal_sql(row_key_column)}",
        ]
        if self.tokenizer:
            options.append(f"{SQLITE_FULL_TEXT_TOKENIZE_OPTION}={literals.get_string_literal_sql(self.tokenizer)}")
        column_list = ", ".join(columns)
        delete_command = literals.get_string_literal_sql(SQLITE_FULL_TEXT_DELETE_COMMAND)
        rebuild_command = literals.get_string_literal_sql(SQLITE_FULL_TEXT_REBUILD_COMMAND)
        new_values = ", ".join(f"new.{column}" for column in columns)
        old_values = ", ".join(f"old.{column}" for column in columns)
        quoted_row_key = quote(row_key_column)
        insert_row_sql = (
            f"INSERT INTO {index_table} (rowid, {column_list}) VALUES (new.{quoted_row_key}, {new_values});"  # nosec B608
        )
        delete_row_sql = (
            f"INSERT INTO {index_table} ({index_table}, rowid, {column_list}) "  # nosec B608
            f"VALUES ({delete_command}, old.{quoted_row_key}, {old_values});"
        )
        updated_columns = ", ".join(dict.fromkeys([quoted_row_key, *columns]))
        return [
            f"CREATE VIRTUAL TABLE {exists}{index_table} USING {SQLITE_FULL_TEXT_MODULE}"
            f"({column_list}, {', '.join(options)});",
            f"CREATE TRIGGER {exists}{quote(table_name + SQLITE_FULL_TEXT_INSERT_TRIGGER_SUFFIX)} "
            f"AFTER INSERT ON {indexed_table} BEGIN {insert_row_sql} END;",
            f"CREATE TRIGGER {exists}{quote(table_name + SQLITE_FULL_TEXT_DELETE_TRIGGER_SUFFIX)} "
            f"AFTER DELETE ON {indexed_table} BEGIN {delete_row_sql} END;",
            f"CREATE TRIGGER {exists}{quote(table_name + SQLITE_FULL_TEXT_UPDATE_TRIGGER_SUFFIX)} "
            f"AFTER UPDATE OF {updated_columns} ON {indexed_table} BEGIN {delete_row_sql} {insert_row_sql} END;",
            f"INSERT INTO {index_table} ({index_table}) VALUES ({rebuild_command});",  # nosec B608
        ]

    def get_drop_sqls(
        self, schema_editor: BaseSchemaEditor, table_name: str, column_names: Sequence[str]
    ) -> list[str]:
        """Returns the statements dropping the FTS5 table and its triggers, each only when it exists."""
        table_name = self.get_index_table_name(table_name, column_names)
        quote = schema_editor.quote
        return [
            *(
                f"DROP TRIGGER IF EXISTS {quote(table_name + suffix)};"
                for suffix in (
                    SQLITE_FULL_TEXT_INSERT_TRIGGER_SUFFIX,
                    SQLITE_FULL_TEXT_DELETE_TRIGGER_SUFFIX,
                    SQLITE_FULL_TEXT_UPDATE_TRIGGER_SUFFIX,
                )
            ),
            f"DROP TABLE IF EXISTS {quote(table_name)};",
        ]

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        if self.tokenizer:
            kwargs["tokenizer"] = self.tokenizer
        return path, args, kwargs

    def __repr__(self) -> str:
        tokenizer = f", tokenizer={self.tokenizer!r}" if self.tokenizer else ""
        return f"{super().__repr__()[:-1]}{tokenizer})"

    def __hash__(self) -> int:
        return hash((super().__hash__(), self.tokenizer))
