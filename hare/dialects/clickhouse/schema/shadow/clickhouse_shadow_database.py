from __future__ import annotations

import dataclasses
import secrets
from collections.abc import Collection
from contextlib import AsyncExitStack
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self, cast

from hare.core.resource_completion import ResourceCompletion
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.base.schema.shadow.shadow_database import ShadowDatabase
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.cluster.constants import (
    CLICKHOUSE_REPLICATED_DATABASE_ENGINE,
)
from hare.dialects.clickhouse.introspection.clickhouse_catalog_engine import ClickhouseCatalogEngine
from hare.dialects.clickhouse.introspection.clickhouse_catalog_identities import ClickhouseCatalogIdentities
from hare.dialects.clickhouse.introspection.constants import (
    CLICKHOUSE_CURRENT_DATABASE_SQL_FUNCTION,
    CLICKHOUSE_DISTRIBUTED_ENGINE_NAME,
    CLICKHOUSE_REPLICA_DEFAULTS_SQL,
)
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_SHADOW_DATABASE_ENGINE_SQL,
    CLICKHOUSE_SHADOW_DATABASE_PREFIX,
    CLICKHOUSE_SHADOW_OTHER_DATABASE_ENGINE_SQL,
)
from hare.dialects.clickhouse.schema.messages import ENGINE_DISTRIBUTED_SHADOW_TARGET
from hare.dialects.clickhouse.schema_objects.clickhouse_materialized_view import ClickhouseMaterializedView
from hare.exceptions import UnSupportedError

if TYPE_CHECKING:
    from hare.ddl.schema_objects.materialized_view import MaterializedView
    from hare.ddl.table_options import TableOptions
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.clickhouse.clickhouse_engine import ClickhouseEngine
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
        self.replica_defaults: dict[str, str] = {}
        self.cleanup = AsyncExitStack()
        #: The shadow databases created, each with the database it stands for - dropped on leaving.
        self.real_database_by_shadow_database: dict[str, str] = {}

    async def __aenter__(self) -> Self:
        rows = await self.connection.execute_dicts(CLICKHOUSE_SHADOW_DATABASE_ENGINE_SQL)
        self.database = str(rows[0]["name"])
        self.replica_defaults = {
            str(row["name"]): str(row["value"])
            for row in await self.connection.execute_dicts(CLICKHOUSE_REPLICA_DEFAULTS_SQL)
        }
        try:
            await ResourceCompletion.wait(self.create_shadow_database(self.prefix, self.database, rows))
            for schema in self.schemas:
                if schema and schema != self.database:
                    schema_rows = await self.connection.execute_dicts(
                        CLICKHOUSE_SHADOW_OTHER_DATABASE_ENGINE_SQL, [schema]
                    )
                    await ResourceCompletion.wait(
                        self.create_shadow_database(f"{self.prefix}_{schema}", schema, schema_rows)
                    )
            self.client = self.connection.create_independent_client({"database": self.prefix})
            self.cleanup.push_async_callback(self.client.close)
            async with self.client.acquire_connection():
                pass
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
        client = cast("ClickhouseClient", self.connection)
        engine_sql = ""
        if engine_rows and engine_rows[0]["engine"] == CLICKHOUSE_REPLICATED_DATABASE_ENGINE:
            engine_sql = ClickhouseCatalogEngine.get_shadow_database_sql(
                str(engine_rows[0]["engine_full"]), self.prefix, self.connection.dialect.literals
            )
        await self.connection.execute_script(
            client.get_database_create_sql(shadow_database, safe=False, engine_sql=engine_sql)
        )
        self.cleanup.push_async_callback(
            self.connection.execute_script, client.get_database_drop_sql(shadow_database, sync=True)
        )
        self.real_database_by_shadow_database[shadow_database] = database

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await ResourceCompletion.wait(self.cleanup.__aexit__(exception_type, exception, traceback))

    def get_shadow_schema_name(self, schema: str | None) -> str | None:
        if schema is None or schema == self.database:
            return self.prefix
        return f"{self.prefix}_{schema}"

    def get_shadow_table_options(self, options: TableOptions) -> TableOptions:
        if not isinstance(options, ClickhouseTableOptions):
            return options
        engine = self.get_shadow_engine(options.get_engine())
        return dataclasses.replace(options, engine=engine)

    def get_shadow_materialized_view(self, view: MaterializedView) -> MaterializedView:
        declaration = ClickhouseMaterializedView.from_materialized_view(view)
        if declaration.to is not None:
            return declaration
        return dataclasses.replace(declaration, engine=self.get_shadow_engine(declaration.get_engine()))

    def get_shadow_engine(self, engine: ClickhouseEngine) -> ClickhouseEngine:
        """Isolate declared storage paths and databases referenced by a routing table.

        A Distributed table must route to a database created for this shadow. Opaque
        targets cannot establish that boundary and are refused before table creation.
        The canonical currentDatabase() argument already follows the shadow client.
        """
        engine = engine.for_shadow(self.prefix, self.replica_defaults)
        if engine.name != CLICKHOUSE_DISTRIBUTED_ENGINE_NAME:
            return engine
        if engine.sql is not None or len(engine.arguments) < 3:
            raise UnSupportedError(ENGINE_DISTRIBUTED_SHADOW_TARGET)
        database = engine.arguments[1]
        if isinstance(database, RawSQLTerm) and database.sql == CLICKHOUSE_CURRENT_DATABASE_SQL_FUNCTION:
            return engine
        databases = {real: shadow for shadow, real in self.real_database_by_shadow_database.items()}
        if not isinstance(database, str) or database not in databases:
            raise UnSupportedError(ENGINE_DISTRIBUTED_SHADOW_TARGET)
        _, arguments, options = engine.deconstruct()
        arguments[2] = databases[database]
        return type(engine)(*arguments, **options)

    def get_catalog_identities(self) -> ClickhouseCatalogIdentities:
        return ClickhouseCatalogIdentities(
            self.real_database_by_shadow_database, self.prefix, self.connection.dialect.literals
        )
