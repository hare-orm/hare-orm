from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.dictionary import Dictionary
from hare.dialects.base.schema.schema_objects.dictionaries import Dictionaries
from hare.dialects.clickhouse.cluster.clickhouse_placement_plan import ClickhousePlacementPlan
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_DICTIONARY_CREATE_TEMPLATE,
    CLICKHOUSE_DICTIONARY_DROP_TEMPLATE,
    CLICKHOUSE_DICTIONARY_LIFETIME_TEMPLATE,
    CLICKHOUSE_DICTIONARY_RELOAD_TEMPLATE,
    CLICKHOUSE_DICTIONARY_RENAME_TEMPLATE,
)
from hare.dialects.clickhouse.schema_objects.clickhouse_dictionary import ClickhouseDictionary
from hare.dialects.clickhouse.schema_objects.dictionary_source import DictionarySource
from hare.exceptions import ConfigurationError
from hare.models import Model

if TYPE_CHECKING:
    from hare.dialects.clickhouse.schema.clickhouse_schema_editor import ClickhouseSchemaEditor


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
            on_cluster=self.editor.get_schema_cluster_sql(model._meta.schema),
            or_replace=or_replace,
            exists=exists,
            dictionary=self.editor.qualify_object_name(model, dictionary.name),
            columns=", ".join(column_sqls),
            key=", ".join(quote(meta.fields_db_projection[field_name]) for field_name in dictionary.key),
            source=self.get_source_sql(model, dictionary),
            layout=dictionary.layout,
            lifetime=""
            if dictionary.keeps_no_rows()
            else CLICKHOUSE_DICTIONARY_LIFETIME_TEMPLATE.format(
                lifetime_minimum=lifetime_minimum, lifetime_maximum=lifetime_maximum
            ),
        )

    def get_source_sql(self, model: type[Model], dictionary: ClickhouseDictionary) -> str:
        """Render explicit source options at execution; previews redact values and raw SQL stays literal."""
        if isinstance(dictionary.source, RawSQLTerm):
            return dictionary.source.sql
        client = self.editor.client
        source = dictionary.source
        if source is None:
            source = DictionarySource(
                "CLICKHOUSE",
                db=model._meta.schema or client.database or "",
                table=model._meta.db_table,
                user=client.user,
                password=client.password,
            )
        return source.get_sql(client.dialect.literals, preview=self.editor.collect_sql)

    async def drop_dictionary(self, model: type[Model], dictionary: Dictionary) -> None:
        await self.editor.run_sql(
            CLICKHOUSE_DICTIONARY_DROP_TEMPLATE.format(
                on_cluster=self.editor.get_schema_cluster_sql(model._meta.schema),
                if_exists="",
                dictionary=self.editor.qualify_object_name(model, dictionary.name),
            )
        )

    def get_model_drop_sqls(self, model: type[Model]) -> list[str]:
        """Compile dependent-object removal before changing the table."""
        if not model._meta.dictionaries:
            return []
        on_cluster = self.editor.get_schema_cluster_sql(model._meta.schema)
        return [
            CLICKHOUSE_DICTIONARY_DROP_TEMPLATE.format(
                on_cluster=on_cluster,
                if_exists="IF EXISTS ",
                dictionary=self.editor.qualify_object_name(model, declaration.name),
            )
            for declaration in reversed(model._meta.dictionaries)
        ]

    async def alter_dictionary(
        self, model: type[Model], old_dictionary: Dictionary, new_dictionary: Dictionary
    ) -> None:
        """Replaces the dictionary at once - a query reads the old one until the new one stands."""
        await self.editor.run_sql(self.get_dictionary_sql(model, new_dictionary, or_replace="OR REPLACE "))

    def get_retyped_field_dictionary_sqls(
        self, old_model: type[Model], new_model: type[Model], field_name: str
    ) -> list[str]:
        """Replaces the dictionaries of a model reading a field a change declares with another column type - a
        dictionary keeps each attribute's type as it was declared.

        Args:
            old_model: The model the table matched.
            new_model: The model it is changed to.
            field_name: The changed field.
        """
        if field_name not in old_model._meta.fields_db_projection or not any(
            field_name in dictionary.get_field_names() for dictionary in new_model._meta.dictionaries
        ):
            return []
        if not self.editor.column_definitions.changes_column_type(old_model, new_model, field_name):
            return []
        return [
            self.get_dictionary_sql(new_model, dictionary, or_replace="OR REPLACE ")
            for dictionary in new_model._meta.dictionaries
            if field_name in dictionary.get_field_names()
        ]

    async def rename_dictionary(
        self, model: type[Model], old_dictionary: Dictionary, new_dictionary: Dictionary
    ) -> None:
        if old_dictionary.name == new_dictionary.name:
            return
        await self.editor.run_sql(
            CLICKHOUSE_DICTIONARY_RENAME_TEMPLATE.format(
                on_cluster=self.editor.get_schema_cluster_sql(model._meta.schema),
                dictionary=self.editor.qualify_object_name(model, old_dictionary.name),
                new_dictionary=self.editor.qualify_object_name(model, new_dictionary.name),
            )
        )

    async def reload_dictionary(self, model: type[Model], dictionary: Dictionary) -> None:
        # Named in its database: every server of a cluster runs the reload, a bare name in its default one.
        await self.editor.run_sql(
            CLICKHOUSE_DICTIONARY_RELOAD_TEMPLATE.format(
                on_cluster=ClickhousePlacementPlan.get_cluster_sql(
                    self.editor.client.sql_context.cluster_name, self.editor.client.sql_context
                ),
                dictionary=self.editor.qualify_table_name(
                    dictionary.name, model._meta.schema or self.editor.client.database
                ),
            )
        )
