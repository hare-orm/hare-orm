from __future__ import annotations

import dataclasses
import os
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.dictionary import Dictionary
from hare.dialects.clickhouse.introspection.clickhouse_sql_parts import ClickhouseSqlParts
from hare.dialects.clickhouse.introspection.constants import (
    CLICKHOUSE_DICTIONARIES_SQL,
    CLICKHOUSE_DICTIONARY_LAYOUT_CLAUSE,
    CLICKHOUSE_DICTIONARY_LIFETIME_CLAUSE,
    CLICKHOUSE_DICTIONARY_PASSWORD_PATTERN,
    CLICKHOUSE_DICTIONARY_SECONDS_PATTERN,
    CLICKHOUSE_DICTIONARY_SOURCE_CLAUSE,
)
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_DICTIONARY_ENVIRONMENT_PATTERN,
    CLICKHOUSE_DICTIONARY_HIDDEN_PASSWORD,
    CLICKHOUSE_DICTIONARY_TABLE_SOURCE_TEMPLATE,
)
from hare.dialects.clickhouse.schema_objects.clickhouse_dictionary import ClickhouseDictionary

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.clickhouse.client.clickhouse_client import ClickhouseClient
    from hare.inspectdb.introspection.table_info import TableInfo


class ClickhouseDeclaredDictionaries:
    """The dictionaries a model declares as ClickHouse has them: one the database lacks is left out,
    one of another key, other attributes, another layout, lifetime or source comes with those of the
    database - the server shows no password of a source, so a source is compared without it."""

    @classmethod
    async def fetch(
        cls,
        connection: DatabaseClient,
        schema: str,
        dictionaries: Sequence[Dictionary],
        table_info: TableInfo,
        column_to_field_name: Mapping[str, str],
    ) -> tuple[Dictionary, ...]:
        """Returns the dictionaries of a model as the database has them.

        Args:
            connection: The connection.
            schema: The database of the model.
            dictionaries: The dictionaries the model declares.
            table_info: The model's table.
            column_to_field_name: Column name -> the name of the model field owning it.

        Returns:
            The dictionaries.
        """
        literals = connection.dialect.literals
        names_sql = ", ".join(literals.get_string_literal_sql(dictionary.name) for dictionary in dictionaries)
        if not names_sql:
            return ()
        rows = await connection.execute_dicts(CLICKHOUSE_DICTIONARIES_SQL.format(names=names_sql), [schema])
        rows_by_name = {str(row["name"]): row for row in rows}
        table_source_sql = CLICKHOUSE_DICTIONARY_TABLE_SOURCE_TEMPLATE.format(
            database=literals.get_string_literal_sql(schema),
            table=literals.get_string_literal_sql(table_info.name),
            user=literals.get_string_literal_sql(cast("ClickhouseClient", connection).user),
            password=literals.get_string_literal_sql(CLICKHOUSE_DICTIONARY_HIDDEN_PASSWORD),
        )
        return tuple(
            cls.get_observed_dictionary(
                dictionary, rows_by_name[dictionary.name], table_source_sql, column_to_field_name
            )
            for dictionary in dictionaries
            if dictionary.name in rows_by_name
        )

    @classmethod
    def get_observed_dictionary(
        cls,
        dictionary: Dictionary,
        row: dict[str, Any],
        table_source_sql: str,
        column_to_field_name: Mapping[str, str],
    ) -> Dictionary:
        """A declared dictionary with what the database keeps of it, where that isn't the declared.

        Args:
            dictionary: The declared dictionary.
            row: The dictionary's key and attributes, with its definition.
            table_source_sql: The source of a dictionary of the model's own table.
            column_to_field_name: Column name -> the name of the model field owning it.

        Returns:
            The dictionary itself when the database keeps it as declared.
        """
        if not isinstance(dictionary, ClickhouseDictionary):
            return dictionary
        definition_sql = str(row["create_table_query"])
        seconds = [
            int(number)
            for number in CLICKHOUSE_DICTIONARY_SECONDS_PATTERN.findall(
                cls.get_clause(definition_sql, CLICKHOUSE_DICTIONARY_LIFETIME_CLAUSE)
            )
        ]
        # ``LIFETIME(300)`` is the greatest alone, ``LIFETIME(MIN 0 MAX 300)`` both.
        lifetime_range = (seconds[0] if len(seconds) > 1 else 0, seconds[-1] if seconds else 0)
        observed_source_sql = cls.get_clause(definition_sql, CLICKHOUSE_DICTIONARY_SOURCE_CLAUSE)
        declared_source_sql = table_source_sql if dictionary.source is None else dictionary.source.sql
        observed: dict[str, Any] = {
            "key": tuple(column_to_field_name.get(name, name) for name in row["key_names"]),
            "attributes": tuple(column_to_field_name.get(name, name) for name in row["attribute_names"]),
            "layout": cls.get_clause(definition_sql, CLICKHOUSE_DICTIONARY_LAYOUT_CLAUSE),
            "lifetime": lifetime_range if lifetime_range[0] else lifetime_range[1],
            "source": RawSQLTerm(observed_source_sql) if observed_source_sql else None,
        }
        same = {
            "key": dictionary.key == observed["key"],
            "attributes": dictionary.attributes == observed["attributes"],
            "layout": cls.get_compared_sql(dictionary.layout) == cls.get_compared_sql(observed["layout"]),
            "lifetime": dictionary.get_lifetime_range() == lifetime_range,
            "source": cls.sources_are_same(declared_source_sql, observed_source_sql),
        }
        if all(same.values()):
            return dictionary
        return dataclasses.replace(
            dictionary, **{option: value for option, value in observed.items() if not same[option]}
        )

    @staticmethod
    def get_clause(definition_sql: str, clause: str) -> str:
        """What a clause of a dictionary's definition holds in its parentheses.

        Args:
            definition_sql: The dictionary's ``CREATE DICTIONARY``, as the server keeps it.
            clause: The clause's word - ``SOURCE``.

        Returns:
            The text in the parentheses; empty without the clause.
        """
        index = ClickhouseSqlParts.find_words(definition_sql, clause)
        if index < 0:
            return ""
        return ClickhouseSqlParts.get_parenthesised(definition_sql[index + len(clause) :])[0].strip()

    @staticmethod
    def get_compared_sql(sql: str) -> str:
        """SQL two spellings of which are compared by - in capitals, without its spaces."""
        return "".join(sql.upper().split())

    @classmethod
    def sources_are_same(cls, declared_sql: str, observed_sql: str) -> bool:
        """Whether a dictionary is loaded from the declared source - its passwords left out of the
        comparison: the server shows none.

        Args:
            declared_sql: The declared source.
            observed_sql: The source the database keeps.

        Returns:
            Whether both name one source; True when the declared one reads an environment variable
            that isn't set here - the source can't be told then.
        """
        unset_variables = [
            name for name in CLICKHOUSE_DICTIONARY_ENVIRONMENT_PATTERN.findall(declared_sql) if name not in os.environ
        ]
        if unset_variables:
            return True
        declared_sql = CLICKHOUSE_DICTIONARY_ENVIRONMENT_PATTERN.sub(
            lambda match: os.environ[match.group(1)], declared_sql
        )
        hidden_password_sql = f"PASSWORD '{CLICKHOUSE_DICTIONARY_HIDDEN_PASSWORD}'"
        return cls.get_compared_sql(
            CLICKHOUSE_DICTIONARY_PASSWORD_PATTERN.sub(hidden_password_sql, declared_sql)
        ) == cls.get_compared_sql(CLICKHOUSE_DICTIONARY_PASSWORD_PATTERN.sub(hidden_password_sql, observed_sql))
