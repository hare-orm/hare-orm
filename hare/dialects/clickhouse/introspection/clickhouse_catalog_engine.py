from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.clickhouse.clickhouse_engine import ClickhouseEngine
from hare.dialects.clickhouse.introspection.clickhouse_sql_parts import ClickhouseSqlParts
from hare.dialects.clickhouse.introspection.constants import (
    CLICKHOUSE_CURRENT_DATABASE_SQL_FUNCTION,
    CLICKHOUSE_ENGINE_COLUMN_ARGUMENTS,
)
from hare.dialects.clickhouse.schema.messages import ENGINE_UNKNOWN_DATABASE_PATH
from hare.exceptions import UnSupportedError
from hare.query.expressions import F

if TYPE_CHECKING:
    from hare.dialects.base.literals.sql_literals import SqlLiterals


class ClickhouseCatalogEngine:
    """Reconstruct recognized server engine grammar, retaining unknown SQL verbatim.

    Only engine-specific column positions become field references. Graphite configuration
    strings and Distributed cluster/database/table names are literals. Unrecognized
    arguments remain raw, without inferred dependencies.
    """

    @staticmethod
    def get(sql: str, default_database: str | None = None) -> str | ClickhouseEngine:
        name = ClickhouseSqlParts.get_before_parentheses(sql)
        arguments_sql, suffix = ClickhouseSqlParts.get_parenthesised(sql)
        arguments = ClickhouseSqlParts.split(arguments_sql)
        engine = ClickhouseEngine(name)
        family = engine.get_family()
        options: dict[str, Any] = {}
        if suffix.strip():
            return ClickhouseEngine(name, sql=RawSQLTerm(sql))
        if not arguments:
            return name
        if (
            engine.is_replicated()
            and arguments[0].strip().startswith("'")
            and not (family == "GraphiteMergeTree" and len(arguments) == 1)
        ):
            if len(arguments) < 2 or not arguments[1].strip().startswith("'"):
                return ClickhouseEngine(name, sql=RawSQLTerm(sql))
            options.update(
                replica_path=ClickhouseSqlParts.get_string_text(arguments.pop(0).strip()),
                replica_name=ClickhouseSqlParts.get_string_text(arguments.pop(0).strip()),
            )
        values: list[Any] = []
        if family in CLICKHOUSE_ENGINE_COLUMN_ARGUMENTS:
            for argument in arguments:
                identifier = ClickhouseSqlParts.get_name_argument(argument.strip())
                if identifier is None:
                    return ClickhouseEngine(name, sql=RawSQLTerm(sql), **options)
                values.append(F(identifier))
        elif family in ("SummingMergeTree", "CoalescingMergeTree"):
            for argument in arguments:
                columns = [
                    ClickhouseSqlParts.get_identifier(item.strip())
                    for item in ClickhouseSqlParts.split(ClickhouseSqlParts.get_unwrapped(argument))
                ]
                if any(column is None for column in columns):
                    return ClickhouseEngine(name, sql=RawSQLTerm(sql), **options)
                values.append(tuple(F(column) for column in columns))  # type: ignore[arg-type]
        elif family == "GraphiteMergeTree":
            if len(arguments) != 1 or not arguments[0].strip().startswith("'"):
                return ClickhouseEngine(name, sql=RawSQLTerm(sql), **options)
            values = [ClickhouseSqlParts.get_string_text(arguments[0].strip())]
        elif name == "Distributed" and len(arguments) >= 3:
            values = [
                ClickhouseSqlParts.get_name_argument(argument) or RawSQLTerm(argument) for argument in arguments[:3]
            ]
            if default_database is not None and values[1] == default_database:
                values[1] = RawSQLTerm(CLICKHOUSE_CURRENT_DATABASE_SQL_FUNCTION)
            values.extend(RawSQLTerm(argument) for argument in arguments[3:])
        elif arguments:
            return ClickhouseEngine(name, sql=RawSQLTerm(sql), **options)
        return ClickhouseEngine(name, *values, **options)

    @staticmethod
    def get_shadow_database_sql(sql: str, prefix: str, literals: SqlLiterals) -> str:
        """Isolate the first path argument of catalog ``Replicated`` database engines."""
        name = ClickhouseSqlParts.get_before_parentheses(sql)
        arguments_sql, suffix = ClickhouseSqlParts.get_parenthesised(sql)
        arguments = ClickhouseSqlParts.split(arguments_sql)
        if name != "Replicated" or len(arguments) != 3 or not arguments[0].strip().startswith("'"):
            raise UnSupportedError(ENGINE_UNKNOWN_DATABASE_PATH)
        path = ClickhouseSqlParts.get_string_text(arguments[0].strip())
        arguments[0] = literals.get_string_literal_sql(f"/{prefix}/{path.lstrip('/')}")
        return f"{name}({', '.join(arguments)}){suffix}"
