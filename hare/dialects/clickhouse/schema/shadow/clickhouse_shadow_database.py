from __future__ import annotations

import dataclasses
import secrets
from collections.abc import Collection
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self, cast

from hare.dialects.base.schema.shadow.shadow_database import ShadowDatabase
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.cluster.constants import (
    CLICKHOUSE_REPLICATED_DATABASE_ENGINE,
    CLICKHOUSE_REPLICATED_ENGINE_PREFIX,
)
from hare.dialects.clickhouse.introspection.clickhouse_sql_parts import ClickhouseSqlParts
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_SHADOW_DATABASE_CREATE_TEMPLATE,
    CLICKHOUSE_SHADOW_DATABASE_DROP_TEMPLATE,
    CLICKHOUSE_SHADOW_DATABASE_ENGINE_SQL,
    CLICKHOUSE_SHADOW_DATABASE_PREFIX,
    CLICKHOUSE_SHADOW_OTHER_DATABASE_ENGINE_SQL,
)
from hare.dialects.clickhouse.types.clickhouse_type_names import ClickhouseTypeNames

if TYPE_CHECKING:
    from hare.ddl.table_options import TableOptions
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.clickhouse.client.clickhouse_client import ClickhouseClient


class ClickhouseShadowDatabase(ShadowDatabase):
    """ShadowDatabase as ClickHouse makes it: a database ``hare_shadow_<hex>`` on every server of the
    connection's cluster - and one more, ``hare_shadow_<hex>_<database>``, for each other database of the
    server a model is in (``Meta.schema``) - each of the ``Replicated`` engine under a Keeper path of its own
    when the database it stands for is one, its replicated tables keeping their rows under Keeper paths of
    their own too, dropped on leaving."""

    def __init__(self, connection: DatabaseClient, schemas: Collection[str]) -> None:
        super().__init__(connection, schemas)
        self.prefix = f"{CLICKHOUSE_SHADOW_DATABASE_PREFIX}{secrets.token_hex(4)}"
        #: The connection's database, read on entering.
        self.database = ""
        #: The shadow databases created, each with the database it stands for - dropped on leaving.
        self.real_database_by_shadow_database: dict[str, str] = {}

    async def __aenter__(self) -> Self:
        rows = await self.connection.execute_dicts(CLICKHOUSE_SHADOW_DATABASE_ENGINE_SQL)
        self.database = str(rows[0]["name"])
        try:
            await self.create_shadow_database(self.prefix, self.database, rows)
            for schema in self.schemas:
                if schema and schema != self.database:
                    schema_rows = await self.connection.execute_dicts(
                        CLICKHOUSE_SHADOW_OTHER_DATABASE_ENGINE_SQL, [schema]
                    )
                    await self.create_shadow_database(f"{self.prefix}_{schema}", schema, schema_rows)
            self.client = self.connection.create_independent_client({"database": self.prefix})
        except BaseException as error:
            # No __aexit__() follows a failed __aenter__() - the shadows created so far are dropped here.
            await self.__aexit__(type(error), error, error.__traceback__)
            raise
        return self

    async def create_shadow_database(
        self, shadow_database: str, database: str, engine_rows: list[dict[str, Any]]
    ) -> None:
        """Creates the shadow of a database of the server - of its ``Replicated`` engine under a Keeper path of
        its own when it is one, of the server's default engine otherwise or when it doesn't exist.

        Args:
            shadow_database: The shadow's name.
            database: The database it stands for.
            engine_rows: The database's row of ``system.databases`` - none when it doesn't exist.
        """
        engine_sql = ""
        if engine_rows and engine_rows[0]["engine"] == CLICKHOUSE_REPLICATED_DATABASE_ENGINE:
            engine_sql = f" ENGINE = {self.get_shadow_engine(str(engine_rows[0]['engine_full']))}"
        await self.connection.execute_script(
            self.get_database_statement(
                CLICKHOUSE_SHADOW_DATABASE_CREATE_TEMPLATE.format(
                    database=self.connection.dialect.literals.quote_identifier(shadow_database), engine=engine_sql
                )
            )
        )
        self.real_database_by_shadow_database[shadow_database] = database

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if self.client is not None:
                await self.client.close()
        finally:
            for shadow_database in self.real_database_by_shadow_database:
                await self.connection.execute_script(
                    self.get_database_statement(
                        CLICKHOUSE_SHADOW_DATABASE_DROP_TEMPLATE.format(
                            database=self.connection.dialect.literals.quote_identifier(shadow_database)
                        )
                    )
                )

    def get_database_statement(self, statement: str) -> str:
        """A statement creating or dropping the shadow database, on every server of the connection's cluster.

        Args:
            statement: ``CREATE DATABASE ...`` or ``DROP DATABASE ...``.

        Returns:
            The statement.
        """
        return cast("ClickhouseClient", self.connection).get_database_statement(statement)

    def get_shadow_schema_name(self, schema: str | None) -> str | None:
        if schema is None or schema == self.database:
            return self.prefix
        return f"{self.prefix}_{schema}"

    def get_shadow_table_options(self, options: TableOptions) -> TableOptions:
        if not isinstance(options, ClickhouseTableOptions) or not options.get_engine_name().startswith(
            CLICKHOUSE_REPLICATED_ENGINE_PREFIX
        ):
            return options
        return dataclasses.replace(options, engine=self.get_shadow_engine(options.engine))

    def get_shadow_engine(self, engine_sql: str) -> str:
        """An engine whose first argument is a Keeper path, the path put under the shadow's own.

        Args:
            engine_sql: The engine, with its arguments.

        Returns:
            The engine - itself without a Keeper path: the server gives each table a path of its own.
        """
        engine_name, arguments = ClickhouseTypeNames.get_type_parts(engine_sql)
        if not arguments or not arguments[0].strip().startswith("'"):
            return engine_sql
        path = ClickhouseSqlParts.get_string_text(arguments[0].strip())
        if not path.startswith("/"):
            return engine_sql
        shadow_path = self.connection.dialect.literals.get_string_literal_sql(f"/{self.prefix}{path}")
        return f"{engine_name}({', '.join([shadow_path, *(argument.strip() for argument in arguments[1:])])})"

    def replace_shadow_names(self, text: str) -> str:
        # The shadows of the other databases first, the longest first - the shadow of the connection's database
        # begins their names; then the Keeper paths - its name begins each of them, and a path's {database} the
        # server filled with it stays the database's.
        for shadow_database, database in sorted(
            self.real_database_by_shadow_database.items(), key=lambda item: -len(item[0])
        ):
            if shadow_database != self.prefix:
                text = text.replace(shadow_database, database)
        return text.replace(f"'/{self.prefix}/", "'/").replace(self.prefix, self.database)
