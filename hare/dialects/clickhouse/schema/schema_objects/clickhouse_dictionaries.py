from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

from hare.ddl.schema_objects.dictionary import Dictionary
from hare.dialects.base.schema.schema_objects.dictionaries import Dictionaries
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_DICTIONARY_CREATE_TEMPLATE,
    CLICKHOUSE_DICTIONARY_DROP_TEMPLATE,
    CLICKHOUSE_DICTIONARY_ENVIRONMENT_PATTERN,
    CLICKHOUSE_DICTIONARY_HIDDEN_PASSWORD,
    CLICKHOUSE_DICTIONARY_RELOAD_TEMPLATE,
    CLICKHOUSE_DICTIONARY_RENAME_TEMPLATE,
    CLICKHOUSE_DICTIONARY_TABLE_SOURCE_TEMPLATE,
)
from hare.dialects.clickhouse.schema_objects.clickhouse_dictionary import ClickhouseDictionary
from hare.exceptions import ConfigurationError
from hare.models import Model

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.clickhouse.schema.declarations import ClickhouseSchemaEditor


class ClickhouseDictionaries(Dictionaries):
    """Dictionaries as ClickHouse writes it - a dictionary's columns are those of the fields it names,
    its source the model's table read by the connection's own user. It isn't dropped with the table it
    reads."""

    editor: ClickhouseSchemaEditor

    __slots__ = ()

    def get_dictionary_create_sqls(self, model: type[Model], dictionary: Dictionary, safe: bool = False) -> list[str]:
        return [self.get_dictionary_sql(model, dictionary, exists="IF NOT EXISTS " if safe else "")]

    def get_dictionary_sql(
        self, model: type[Model], dictionary: Dictionary, *, exists: str = "", or_replace: str = ""
    ) -> str:
        """The statement creating a dictionary.

        Args:
            model: The model declaring it.
            dictionary: The dictionary.
            exists: ``IF NOT EXISTS`` with its trailing space, or nothing.
            or_replace: ``OR REPLACE`` with its trailing space, or nothing.

        Returns:
            The statement.

        Raises:
            ConfigurationError: The dictionary isn't one of ClickHouse's, names no field of the
                model, or its source names an environment variable that isn't set.
        """
        if not isinstance(dictionary, ClickhouseDictionary):
            raise ConfigurationError(
                f"{model.__name__}: Meta.dictionaries takes ClickhouseDictionary(...) on ClickHouse, got "
                f"{dictionary!r}"
            )
        meta = model._meta
        unknown = [name for name in dictionary.get_field_names() if name not in meta.fields_db_projection]
        if unknown:
            raise ConfigurationError(
                f"{model.__name__}: ClickhouseDictionary {dictionary.name!r} names no field {unknown}"
            )
        quote = self.editor.quote
        column_definitions = self.editor.column_definitions
        column_sqls = []
        for field_name in dictionary.get_field_names():
            field = meta.fields_map[field_name]
            column_type = column_definitions.get_altered_column_type(
                column_definitions.get_table_column_type(model, field), field.null
            )
            column_sqls.append(f"{quote(meta.fields_db_projection[field_name])} {column_type}")
        lifetime_minimum, lifetime_maximum = dictionary.get_lifetime_range()
        return CLICKHOUSE_DICTIONARY_CREATE_TEMPLATE.format(
            or_replace=or_replace,
            exists=exists,
            dictionary=self.editor.qualify_object_name(model, dictionary.name),
            columns=", ".join(column_sqls),
            key=", ".join(quote(meta.fields_db_projection[field_name]) for field_name in dictionary.key),
            source=self.get_source_sql(model, dictionary),
            layout=dictionary.layout,
            lifetime_minimum=lifetime_minimum,
            lifetime_maximum=lifetime_maximum,
        )

    def get_source_sql(self, model: type[Model], dictionary: ClickhouseDictionary) -> str:
        """What a dictionary is loaded from, as ``SOURCE(...)`` takes it.

        Args:
            model: The model declaring the dictionary.
            dictionary: The dictionary.

        Returns:
            The declared source, each ``{env:NAME}`` of it replaced by the environment variable; the
            model's table read by the connection's user without one - its password hidden in SQL
            that is collected, not run.

        Raises:
            ConfigurationError: The source names an environment variable that isn't set.
        """
        literals = self.editor.client.dialect.literals
        if dictionary.source is not None:

            def get_variable_sql(match: re.Match[str]) -> str:
                value = os.environ.get(match.group(1))
                if value is None:
                    raise ConfigurationError(
                        f"ClickhouseDictionary {dictionary.name!r}: its source reads the environment variable "
                        f"{match.group(1)}, which isn't set"
                    )
                # Without the quotes of a literal: the source writes its own around the variable.
                return literals.get_string_literal_sql(value)[1:-1]

            return CLICKHOUSE_DICTIONARY_ENVIRONMENT_PATTERN.sub(get_variable_sql, dictionary.source.sql.strip())
        client = self.editor.client
        password = CLICKHOUSE_DICTIONARY_HIDDEN_PASSWORD if self.editor.collect_sql else client.password
        return CLICKHOUSE_DICTIONARY_TABLE_SOURCE_TEMPLATE.format(
            database=literals.get_string_literal_sql(client.database or ""),
            table=literals.get_string_literal_sql(model._meta.db_table),
            user=literals.get_string_literal_sql(client.user),
            password=literals.get_string_literal_sql(password),
        )

    async def drop_dictionary(self, model: type[Model], dictionary: Dictionary) -> None:
        await self.editor.run_sql(
            CLICKHOUSE_DICTIONARY_DROP_TEMPLATE.format(
                if_exists="", dictionary=self.editor.qualify_object_name(model, dictionary.name)
            )
        )

    async def drop_model_dictionaries(self, model: type[Model]) -> None:
        for dictionary in reversed(model._meta.dictionaries):
            await self.editor.run_sql(
                CLICKHOUSE_DICTIONARY_DROP_TEMPLATE.format(
                    if_exists="IF EXISTS ", dictionary=self.editor.qualify_object_name(model, dictionary.name)
                )
            )

    async def alter_dictionary(
        self, model: type[Model], old_dictionary: Dictionary, new_dictionary: Dictionary
    ) -> None:
        """Replaces the dictionary at once - a query reads the old one until the new one stands."""
        await self.editor.run_sql(self.get_dictionary_sql(model, new_dictionary, or_replace="OR REPLACE "))

    async def rename_dictionary(
        self, model: type[Model], old_dictionary: Dictionary, new_dictionary: Dictionary
    ) -> None:
        if old_dictionary.name == new_dictionary.name:
            return
        await self.editor.run_sql(
            CLICKHOUSE_DICTIONARY_RENAME_TEMPLATE.format(
                dictionary=self.editor.qualify_object_name(model, old_dictionary.name),
                new_dictionary=self.editor.qualify_object_name(model, new_dictionary.name),
            )
        )

    async def reload_dictionary(self, model: type[Model], dictionary: Dictionary) -> None:
        await self.editor.run_sql(
            CLICKHOUSE_DICTIONARY_RELOAD_TEMPLATE.format(
                dictionary=self.editor.qualify_object_name(model, dictionary.name)
            )
        )
