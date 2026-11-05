from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING

from sqlparse import tokens
from sqlparse.lexer import tokenize

from hare.dialects.clickhouse.clickhouse_engine import ClickhouseEngine
from hare.dialects.clickhouse.introspection.clickhouse_sql_parts import ClickhouseSqlParts
from hare.dialects.clickhouse.introspection.constants import (
    CLICKHOUSE_DISTRIBUTED_ENGINE_NAME,
    CLICKHOUSE_ENGINE_CLAUSES,
)
from hare.inspectdb.introspection.catalog_identities import CatalogIdentities
from hare.inspectdb.introspection.enums import SchemaPartType

if TYPE_CHECKING:
    from hare.dialects.base.literals.sql_literals import SqlLiterals
    from hare.inspectdb.introspection.schema_part import SchemaPart


class ClickhouseCatalogIdentities(CatalogIdentities):
    """Map database and Keeper identities in recognized engine and local source arguments."""

    def __init__(self, schemas: Mapping[str, str], prefix: str, literals: SqlLiterals) -> None:
        super().__init__(schemas)
        self.prefix = prefix
        self.literals = literals

    def map_engine(self, sql: str) -> str:
        """Preserve opaque engines; map known database arguments and scoped replica paths."""
        name = ClickhouseSqlParts.get_before_parentheses(sql)
        arguments_sql, suffix = ClickhouseSqlParts.get_parenthesised(sql)
        arguments = ClickhouseSqlParts.split(arguments_sql)
        if not arguments:
            return sql
        if name == CLICKHOUSE_DISTRIBUTED_ENGINE_NAME:
            if len(arguments) < 3 or not arguments[1].startswith("'"):
                return sql
            database = ClickhouseSqlParts.get_string_text(arguments[1])
            if database not in self.schemas:
                return sql
            arguments[1] = self.literals.get_string_literal_sql(self.schemas[database])
        elif ClickhouseEngine(name).is_replicated() and arguments[0].startswith("'"):
            path = ClickhouseSqlParts.get_string_text(arguments[0])
            keeper, separator, suffix_path = path.partition(":")
            if separator:
                path = suffix_path
            else:
                keeper = ""
            scoped_prefix = f"/{self.prefix}/"
            if not path.startswith(scoped_prefix):
                return sql
            segments = path[len(scoped_prefix) :].split("/")
            path = keeper + separator + "/" + "/".join(self.schemas.get(segment, segment) for segment in segments)
            arguments[0] = self.literals.get_string_literal_sql(path)
        else:
            return sql
        return f"{name}({', '.join(arguments)}){suffix}"

    def map_part(self, part: SchemaPart) -> SchemaPart:
        """Map recognized catalog storage settings independently of user SQL literals."""
        mapped = super().map_part(part)
        query = mapped.definition.get("query") if isinstance(mapped.definition, dict) else None
        if not isinstance(query, str):
            return mapped
        if part.part_type is SchemaPartType.DICTIONARY:
            return replace(mapped, definition={**mapped.definition, "query": self.map_dictionary_source(query)})
        if part.part_type is not SchemaPartType.MATERIALIZED_VIEW:
            return mapped
        engine_position = ClickhouseSqlParts.find_words(query, "ENGINE")
        select_position = ClickhouseSqlParts.find_words(query, "AS")
        if engine_position < 0 or (select_position >= 0 and select_position < engine_position):
            return mapped
        start = engine_position + len("ENGINE")
        tail = query[start:].lstrip()
        if not tail.startswith("="):
            return mapped
        engine_sql = ClickhouseSqlParts.get_clauses(tail[1:].lstrip(), (*CLICKHOUSE_ENGINE_CLAUSES, "AS"))[""]
        start = query.index(engine_sql, start)
        definition = dict(mapped.definition)
        definition["query"] = query[:start] + self.map_engine(engine_sql) + query[start + len(engine_sql) :]
        return replace(mapped, definition=definition)

    def map_dictionary_source(self, sql: str) -> str:
        """Map only the database literal of a local CLICKHOUSE dictionary source.

        Remote hosts and other source types retain their settings. QUERY, WHERE and other
        string values remain opaque even when they contain a mapped database's name.
        """
        source_position = ClickhouseSqlParts.find_words(sql, "SOURCE")
        if source_position < 0:
            return sql
        source, suffix = ClickhouseSqlParts.get_parenthesised(sql[source_position:])
        if ClickhouseSqlParts.get_before_parentheses(source) != "CLICKHOUSE":
            return sql
        arguments, _ = ClickhouseSqlParts.get_parenthesised(source)
        database_position = ClickhouseSqlParts.find_words(arguments, "DB")
        if database_position < 0 or ClickhouseSqlParts.find_words(arguments, "HOST") >= 0:
            return sql
        start = database_position + len("DB")
        start += len(arguments[start:]) - len(arguments[start:].lstrip())
        token_type, literal = next(tokenize(arguments[start:]), (None, ""))
        if token_type is not tokens.Literal.String.Single:
            return sql
        database = ClickhouseSqlParts.get_string_text(literal)
        if database not in self.schemas:
            return sql
        mapped_literal = self.literals.get_string_literal_sql(self.schemas[database])
        arguments = arguments[:start] + mapped_literal + arguments[start + len(literal) :]
        return sql[:source_position] + f"SOURCE(CLICKHOUSE({arguments}))" + suffix
