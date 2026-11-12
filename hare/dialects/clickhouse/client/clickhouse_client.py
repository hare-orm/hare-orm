from __future__ import annotations

import abc
import asyncio
import contextlib
from collections.abc import AsyncGenerator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.client.declarations import ShellCommand
from hare.dialects.base.connection.connection_options import ConnectionOptions
from hare.dialects.clickhouse.client.clickhouse_connection import ClickhouseConnection
from hare.dialects.clickhouse.client.clickhouse_errors import ClickhouseErrors
from hare.dialects.clickhouse.client.clickhouse_external_sets import ClickhouseExternalSets
from hare.dialects.clickhouse.client.clickhouse_key_series import ClickhouseKeySeries
from hare.dialects.clickhouse.client.clickhouse_mutation_recovery import ClickhouseMutationRecovery
from hare.dialects.clickhouse.client.clickhouse_query_templates import ClickhouseQueryTemplates
from hare.dialects.clickhouse.client.constants import (
    CLICKHOUSE_CLIENT_BASE_SETTINGS,
    CLICKHOUSE_CLOSED_WHILE_OPENING_MESSAGE,
    CLICKHOUSE_CONNECTION_OPTION_DEFAULTS,
    CLICKHOUSE_DEFAULT_DATABASE,
    CLICKHOUSE_DEFAULT_USER,
    CLICKHOUSE_DISCARDED_CLIENT_MESSAGE,
    CLICKHOUSE_KEEPER_CONNECTIONS_SQL,
    CLICKHOUSE_MAX_EXECUTION_TIME_SETTING,
    CLICKHOUSE_ROW_RETURNING_KEYWORDS,
    CLICKHOUSE_ROW_WRITE_PATTERN,
    CLICKHOUSE_SERIES_KEEPER_PATH_SQL,
    CLICKHOUSE_SERVER_FACTS_SQL,
    CLICKHOUSE_SHELL_PASSWORD_VARIABLE,
    CLICKHOUSE_SHELL_PROGRAM,
    CLICKHOUSE_STATEMENT_PREFIX_PATTERN,
    CLICKHOUSE_STREAM_BATCH_SIZE,
)
from hare.dialects.clickhouse.client.declarations import (
    ClickhouseDriverErrors,
    ClickhouseExternalSet,
    ClickhouseValueSet,
)
from hare.dialects.clickhouse.cluster.clickhouse_cluster import ClickhouseCluster
from hare.dialects.clickhouse.cluster.clickhouse_placement_plan import ClickhousePlacementPlan
from hare.dialects.clickhouse.cluster.constants import (
    CLICKHOUSE_CLUSTER_SESSION_SETTINGS,
    CLICKHOUSE_DATABASE_CREATE_TEMPLATE,
    CLICKHOUSE_DATABASE_DROP_TEMPLATE,
)
from hare.dialects.clickhouse.constants import (
    CLICKHOUSE_COMMENT_STARTS,
    CLICKHOUSE_CONNECTION_OPTIONS,
    CLICKHOUSE_DEFAULT_HTTP_PORT,
    CLICKHOUSE_DIALECT,
    CLICKHOUSE_STATEMENT_END_PATTERN,
)
from hare.dialects.clickhouse.drivers.constants import (
    CLICKHOUSE_SESSION_SETTINGS,
    CLICKHOUSE_UNKNOWN_DATABASE_ERROR_CODES,
)
from hare.dialects.clickhouse.keeper.clickhouse_database_keeper_nodes import ClickhouseDatabaseKeeperNodes
from hare.dialects.clickhouse.keeper.clickhouse_migration_lock import ClickhouseMigrationLock
from hare.dialects.clickhouse.query.clickhouse_sql_context import ClickhouseSqlContext
from hare.dialects.clickhouse.query.declarations import ClickhouseQuery
from hare.dialects.clickhouse.types.declarations import ClickhouseTypedValue
from hare.exceptions import ConfigurationError, DBConnectionError, UnSupportedError

if TYPE_CHECKING:
    from hare.models import Model
    from hare.sql.builder.tables.table import Table


class ClickhouseClient(DatabaseClient):
    """What any ClickHouse driver shares: the connection settings, the parameters written into the
    statement as literals, the errors told apart by ClickHouse's error codes. A statement stands alone
    unless the connection takes transactions (``transactions=true``)."""

    dialect = CLICKHOUSE_DIALECT
    query_class = ClickhouseQuery

    #: Whether the driver reads a JSON column - else a JSONField is stored as its text on any server.
    reads_json_type: ClassVar[bool] = True

    async def _post_connect(self) -> None:
        await super()._post_connect()
        if self.cluster is not None and not self.is_transaction_client:
            await self.cluster.refresh_databases()
        # What a connection of its own learns of the server, read in one statement.
        server_facts = (
            None if self.is_transaction_client else (await self.execute_dicts(CLICKHOUSE_SERVER_FACTS_SQL))[0]
        )
        if (
            server_facts is not None
            and self.features.takes_keys_before_insert
            and not await self.has_keeper(int(server_facts["keeper_tables"]))
        ):
            # The keys of a series need a ClickHouse Keeper.
            self.features = self.features.replace(supports_generated_keys=False, takes_keys_before_insert=False)
        if self.options["keeper_hosts"] and not self.is_transaction_client:
            await self.check_keeper_session()
        if self.options["transactions"] and not self.is_transaction_client:
            try:
                await self.check_transactions()
            except self.driver_errors.driver as error:
                await self.close_opened_connection()
                raise ConfigurationError(
                    f"{self.connection_alias!r} asks for transactions (transactions=true), which the server doesn't "
                    f"run - it needs a ClickHouse Keeper and allow_experimental_transactions: {error}"
                ) from error

    features = CLICKHOUSE_DIALECT.features
    #: The driver's exception classes.
    driver_errors: ClassVar[ClickhouseDriverErrors]
    #: The port of a connection given none - the one of the protocol the driver speaks.
    default_port: ClassVar[int] = CLICKHOUSE_DEFAULT_HTTP_PORT
    #: The settings the driver's connections take, and the values of the ones left unset.
    connection_options: ClassVar[ConnectionOptions] = CLICKHOUSE_CONNECTION_OPTIONS
    connection_option_defaults: ClassVar[Mapping[str, Any]] = CLICKHOUSE_CONNECTION_OPTION_DEFAULTS

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int | None = None,
        user: str | None = CLICKHOUSE_DEFAULT_USER,
        password: str | None = "",  # nosec B107 - no password, as ClickHouse's default user
        database: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(host=host, port=port, user=user, password=password, database=database, **kwargs)
        settings = {name: kwargs.pop(name) for name in list(kwargs) if name not in CLICKHOUSE_CLIENT_BASE_SETTINGS}
        if not isinstance(host, str) or not host:
            raise ConfigurationError(f"A ClickHouse connection needs a host, got {host!r}")
        if port is None:
            port = self.default_port
        if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
            raise ConfigurationError(f"A ClickHouse connection's port must be 1 to 65535, got {port!r}")
        if database is not None and (not isinstance(database, str) or not database):
            raise ConfigurationError(f"A ClickHouse connection's database must be a non-empty name, got {database!r}")
        self.host = host
        self.port = port
        # A URL without a user or a password names none - None, which the drivers take for no text.
        self.user = user or CLICKHOUSE_DEFAULT_USER
        self.password = password or ""
        self.database = database
        #: The checked connection settings, by name.
        self.options: dict[str, Any] = self.connection_options.read(settings, self.connection_option_defaults)
        self.connection_options.raise_for_unknown(settings, self.driver_name)
        if self.options["transactions"]:
            # Set before the connection opens - a transaction may begin first, on a new connection
            # (autonomous()) always; the opening then checks the server runs them. They have no
            # savepoints: a nested atomic() joins the one it is nested in.
            self.features = self.features.replace(supports_transactions=True, supports_savepoints=False)
        self._connection: Any = None
        #: Held while the connection is opened - the statements starting together open one.
        self._connection_lock = asyncio.Lock()
        self.closes_started = 0
        self.opening_task = None
        #: The key series this process has moved past the greatest key of their tables, by name.
        self.synchronized_key_series: set[str] = set()
        #: The cluster the connection belongs to - None for a connection to a server of its own.
        self.cluster: ClickhouseCluster | None = (
            None if self.options["cluster"] is None else ClickhouseCluster(self, self.options["cluster"])
        )
        self._apply_json_column_variant()

    def _apply_json_column_variant(self) -> None:
        """Picks the dialect a JSONField is a JSON column in where the driver reads one - before any
        connection, so the SQL printed without one (``sqlmigrate``) is the SQL run."""
        if not self.reads_json_type:
            self.features = self.features.replace(supports_json_type=False)
        # Local import: the variants module builds query classes, which import the client's package.
        from hare.dialects.clickhouse.query.clickhouse_dialect_variants import ClickhouseDialectVariants

        dialect, query_class = ClickhouseDialectVariants.get(self.features.supports_json_type)
        if self.dialect is not dialect:
            self.dialect = dialect
            self.query_class = query_class

    def get_session_settings(self) -> dict[str, Any]:
        """The settings every statement of the connection runs with.

        Returns:
            The settings, by name - the server stopping a statement running past
            ``send_receive_timeout`` (the native protocol's timeout bounds a wait for the server's
            next packet, which a running statement sends), and asynchronous inserts where the
            connection asks for them: the server gathers the inserted rows of many statements into
            one part; without waiting for the write, a statement returns before its rows are written
            and their errors are lost.
        """
        settings = {
            **CLICKHOUSE_SESSION_SETTINGS,
            CLICKHOUSE_MAX_EXECUTION_TIME_SETTING: self.options["send_receive_timeout"],
        }
        if self.options["async_insert"]:
            settings["async_insert"] = 1
            settings["wait_for_async_insert"] = int(self.options["wait_for_async_insert"])
        else:
            # ClickHouse 26.3+ inserts asynchronously unless told otherwise.
            settings["async_insert"] = 0
        if self.cluster is not None:
            settings.update(CLICKHOUSE_CLUSTER_SESSION_SETTINGS)
        return settings

    async def run_command_without_database(self, statement: str) -> None:
        """Runs a statement on the connection opened without a database (``create_connection(with_db=False)``)
        - the driver's own call.

        Args:
            statement: The statement.
        """
        raise NotImplementedError

    async def db_create(self) -> None:
        await self.create_connection(with_db=False)
        try:
            await self.run_command_without_database(self.get_database_create_sql(self.database or ""))
        finally:
            await self.close()

    async def db_delete(self) -> None:
        """Drops the connection's database - with ``keeper_hosts``, also its nodes of ClickHouse Keeper: the
        series of its generated keys and the node of its migration lock."""
        await self.close()
        await self.create_connection(with_db=False)
        try:
            await self.run_command_without_database(self.get_database_drop_sql(self.database or ""))
        except self.driver_errors.server as error:
            if self.get_error_code(error) not in CLICKHOUSE_UNKNOWN_DATABASE_ERROR_CODES:
                raise
        else:
            if self.options["keeper_hosts"]:
                series_keeper_path = (await self.execute_dicts(CLICKHOUSE_SERIES_KEEPER_PATH_SQL))[0]
                await ClickhouseDatabaseKeeperNodes(
                    ClickhouseMigrationLock.get_addresses(self.options["keeper_hosts"]),
                    self.database or CLICKHOUSE_DEFAULT_DATABASE,
                    self.options["connect_timeout"],
                ).delete(series_keeper_path["series_keeper_path"] or "")
        finally:
            await self.close()

    def get_database_create_sql(self, database: str, *, safe: bool = True, engine_sql: str = "") -> str:
        """Render an explicit database creation target; engine SQL is a catalog/declaration fragment."""
        context = self.sql_context
        return CLICKHOUSE_DATABASE_CREATE_TEMPLATE.format(
            if_not_exists="IF NOT EXISTS " if safe else "",
            database=context.quote(database),
            on_cluster=ClickhousePlacementPlan.get_cluster_sql(context.cluster_name, context),
            engine=f" ENGINE = {engine_sql}" if engine_sql else "",
        )

    def get_database_drop_sql(self, database: str, *, safe: bool = True, sync: bool = False) -> str:
        """Render an explicit database removal target without parsing a statement."""
        context = self.sql_context
        return CLICKHOUSE_DATABASE_DROP_TEMPLATE.format(
            if_exists="IF EXISTS " if safe else "",
            database=context.quote(database),
            on_cluster=ClickhousePlacementPlan.get_cluster_sql(context.cluster_name, context),
            sync=" SYNC" if sync else "",
        )

    async def invalidate_schema_cache(self) -> None:
        """Refresh placement facts at the explicit schema boundary, including failed raw DDL."""
        if self.cluster is not None:
            await self.cluster.refresh_databases()

    def get_write_recovery(self, table: Table) -> ClickhouseMutationRecovery | None:
        """Keep the same explicit storage placement for execution failure handling."""
        placement = ClickhousePlacementPlan.for_table(table, self.sql_context)
        return ClickhouseMutationRecovery.for_mutation(placement, self.database or CLICKHOUSE_DEFAULT_DATABASE)

    @property
    def sql_context(self) -> ClickhouseSqlContext:
        return ClickhouseSqlContext.from_context(super().sql_context).copy(
            cluster_name=self.cluster.name if self.cluster is not None else None
        )

    @property
    def sql_context_key(self) -> object:
        return self.cluster.name if self.cluster is not None else None

    @staticmethod
    def is_row_write_sql(sql: str) -> bool:
        """Classify a driver command without changing its target or cluster.

        ORM mutations already carry their placement from compilation. User SQL is
        literal, including writes; the driver may choose its command/result path.
        """
        return CLICKHOUSE_ROW_WRITE_PATTERN.match(sql) is not None

    async def _driver_stream_batches(
        self, query: str, values: list[Any] | None = None, chunk_size: int = 0
    ) -> AsyncGenerator[list[Any]]:
        """The rows of a query as the server sends them, a batch at a time - outside a transaction
        too: the server computes the rows while they are read, no cursor holds them."""
        sql = self.get_inlined_query(query, values)
        async with contextlib.aclosing(self.read_batches(sql, chunk_size or CLICKHOUSE_STREAM_BATCH_SIZE)) as batches:
            async for batch in batches:
                yield batch

    @abc.abstractmethod
    def read_batches(self, sql: str, batch_size: int) -> AsyncGenerator[list[Any]]:
        """The rows of a query, read by the driver a batch at a time.

        Args:
            sql: The query, inlined.
            batch_size: How many rows a batch holds.

        Returns:
            The batches, each row by position and by column name.
        """

    def get_address(self) -> str:
        return f"{self.host}:{self.port}"

    async def generate_schema(self, safe: bool) -> None:
        # The tables a migration would refuse are refused here too, before any of them is created.
        table_creation = self.dialect.schema_editor_class(self).table_creation
        for model in table_creation.get_models_to_create():
            await table_creation.get_replica_check_plan(model).execute(table_creation.editor)  # type: ignore[attr-defined]
        await super().generate_schema(safe)

    def acquire_connection(self) -> Any:
        return ClickhouseConnection(self)

    async def open_connection(self) -> Any:
        """Opens the driver's connection unless it is open - anew when a close ran while it opened
        (``Connections.reconnect()``), never for a client closed and dropped.

        Returns:
            The connection.

        Raises:
            DBConnectionError: The client was closed and dropped.
        """
        async with self._connection_lock:
            while self._connection is None:
                if self.discarded:
                    raise DBConnectionError(CLICKHOUSE_DISCARDED_CLIENT_MESSAGE.format(alias=self.connection_alias))
                try:
                    reopened = await self.open_across_closes()
                except Exception as error:
                    # Opened outside a statement too (generate_schemas()) - a missing database, a refused
                    # login are hare's errors there as well.
                    hare_error = ClickhouseClient.translate_driver_error(self, error, None, None, (), False)
                    if hare_error is error:
                        raise
                    raise hare_error from error
                if reopened:
                    # A close ran while it opened - one that found nothing to close yet leaves it here.
                    connection, self._connection = self._connection, None
                    if connection is not None:
                        await connection.close()  # type: ignore[unreachable]
            return self._connection

    async def get_shell_command(self) -> ShellCommand:
        arguments = [CLICKHOUSE_SHELL_PROGRAM, "--host", self.host, "--user", self.user]
        native_port = self.get_native_port()
        if native_port is not None:
            arguments += ["--port", str(native_port)]
        if self.database:
            arguments += ["--database", self.database]
        if self.options["secure"]:
            arguments.append("--secure")
        environment = {CLICKHOUSE_SHELL_PASSWORD_VARIABLE: self.password} if self.password else {}
        return ShellCommand(tuple(arguments), environment)

    def get_native_port(self) -> int | None:
        """The port of the server's native protocol, which the interactive client speaks.

        Returns:
            The port, None for the interactive client's own default.
        """
        return self.options["native_port"]

    async def take_statement_connection(self) -> Any:
        """The driver's connection for one statement.

        Returns:
            The connection, opened on first use.
        """
        connection = self._connection
        if connection is None and self.opening_task is not None and self.opening_task is asyncio.current_task():
            # A setup statement of the connection being opened, closed meanwhile - the opening task
            # holds the lock opening it would wait on, and opens it anew.
            raise DBConnectionError(CLICKHOUSE_CLOSED_WHILE_OPENING_MESSAGE)
        opening_task = self.opening_task
        if connection is not None and (opening_task is None or opening_task is asyncio.current_task()):
            return connection
        # A connection another task still sets up - the server's version and features - is waited for.
        try:
            return await self.open_connection()
        except DBConnectionError:
            successor = self.get_successor()
            if successor is None:
                raise
            # Discarded by a repeated Hare.init() - the statement runs on the client replacing it.
            return await successor.take_statement_connection()  # type: ignore[attr-defined]

    def give_back_statement_connection(self, exception: BaseException | None) -> None:
        """Ends a statement's hold of the connection - nothing to do: the driver's connections run
        statements concurrently.

        Args:
            exception: The error the statement raised, None when it succeeded.
        """

    async def take_generated_keys(self, model: type[Model], count: int) -> list[Any]:
        """The next keys of the model's series - ``generateSerialID`` of the series named after the
        database and the model (``database.app.Model``), which a rename of its table keeps. Before the
        first keys a process takes of a series, the series is moved past the greatest key of the table."""
        return await ClickhouseKeySeries.take_generated_keys(self, model, count)

    async def get_generated_key_sql(self, model: type[Model]) -> str | None:
        """``generateSerialID`` of the model's series - moved past the greatest key of its table first."""
        return await ClickhouseKeySeries.get_generated_key_sql(self, model)

    def get_key_series_name(self, model: type[Model]) -> str:
        """The name of the series of a model's keys - the Keeper keeps one series per name for every
        database of the server, so the database is part of it.

        Args:
            model: The model.

        Returns:
            ``database.app.Model``.
        """
        return ClickhouseKeySeries.get_key_series_name(self, model)

    async def synchronize_key_series(self, model: type[Model], *, checked_once: bool = False) -> None:
        """Moves the model's series past the greatest key of its table - the series hands out the keys
        after it.

        Args:
            model: The model.
            checked_once: Whether a series this process already moved is left alone.
        """
        return await ClickhouseKeySeries.synchronize_key_series(self, model, checked_once=checked_once)

    def hold_migration_lock(self) -> contextlib.AbstractAsyncContextManager[None]:
        # Without keeper_hosts no lock is held: the server has no lock given back when a process dies.
        if not self.options["keeper_hosts"]:
            return contextlib.nullcontext()
        return self.hold_keeper_migration_lock()

    def hold_keeper_migration_lock(self) -> contextlib.AbstractAsyncContextManager[None]:
        """Holds the database's migration lock in the ClickHouse Keeper of ``keeper_hosts``, waiting while
        another ``migrate`` holds it."""
        migration_lock = ClickhouseMigrationLock(
            ClickhouseMigrationLock.get_addresses(self.options["keeper_hosts"]),
            self.database or CLICKHOUSE_DEFAULT_DATABASE,
            self.options["connect_timeout"],
        )
        return migration_lock.hold()

    async def close_opened_connection(self) -> None:
        """Closes the connection a failed setup of it opened - no close of the client, which the opening
        would take for a close meanwhile and open again, failing the same way without end."""
        connection, self._connection = self._connection, None
        if connection is not None:
            await connection.close()

    async def check_keeper_session(self) -> None:
        """Checks that a Keeper server of ``keeper_hosts`` opens a session.

        Raises:
            ConfigurationError: No server accepted the session, addresses are invalid, or the extra is missing.
        """
        migration_lock = ClickhouseMigrationLock(
            ClickhouseMigrationLock.get_addresses(self.options["keeper_hosts"]),
            self.database or CLICKHOUSE_DEFAULT_DATABASE,
            self.options["connect_timeout"],
        )
        try:
            await migration_lock.check()
        except ConfigurationError:
            await self.close_opened_connection()
            raise
        except DBConnectionError as error:
            await self.close_opened_connection()
            raise ConfigurationError(
                f"{self.connection_alias!r} takes its migration lock in ClickHouse Keeper (keeper_hosts="
                f"{self.options['keeper_hosts']!r}), which took no session: {error}"
            ) from error

    async def has_keeper(self, keeper_tables: int) -> bool:
        """Whether the server is connected to a ClickHouse Keeper - a server without one has no table of
        its connections.

        Args:
            keeper_tables: How many tables of Keeper connections the server has.

        Returns:
            Whether it is.
        """
        if not keeper_tables:
            return False
        rows = await self.execute_dicts(CLICKHOUSE_KEEPER_CONNECTIONS_SQL)
        return bool(rows and rows[0]["keepers"])

    @abc.abstractmethod
    async def check_transactions(self) -> None:
        """Checks that the server runs transactions - a ``BEGIN TRANSACTION`` and its ``ROLLBACK`` on
        a connection of their own.

        Raises:
            Exception: The driver's error of the server refusing them.
        """

    def _get_transaction_client(self) -> Any:
        raise UnSupportedError(
            f"{self.connection_alias!r} is a ClickHouse connection with no transactions - its transactions=true "
            "setting turns them on, on a server running them (a ClickHouse Keeper and allow_experimental_transactions)"
        )

    def get_read_query(self, query: str, values: Sequence[Any] | None) -> tuple[str, list[ClickhouseExternalSet]]:
        """A read with each set of values bound as one parameter (``ClickhouseValueSet``) sent as an
        external table - the other values written into it.

        Args:
            query: The statement.
            values: The values, the ``n``-th for ``$n``.

        Returns:
            The statement to send, and the external tables to send beside it.
        """
        if not values or not any(type(value) is ClickhouseValueSet for value in values):
            return self.get_inlined_query(query, values), []
        return ClickhouseExternalSets.get_query(query, values, self.dialect.literals.get_literal_sql)

    def get_inlined_query(self, query: str, values: Sequence[Any] | None) -> str:
        """The statement with each ``$n`` placeholder replaced by its value's literal - a ``$`` inside
        a quoted string or name stays.

        Args:
            query: The statement.
            values: The values, the ``n``-th for ``$n``.

        Returns:
            The statement to send.
        """
        if not values:
            return query
        return ClickhouseQueryTemplates.get_inlined_query(query, values, self.dialect.literals.get_literal_sql)

    @staticmethod
    def returns_rows(query: str) -> bool:
        """Whether a statement returns rows, by its first word - after any spaces, opening brackets
        and comments.

        Args:
            query: The statement.

        Returns:
            True for a query.
        """
        prefix = CLICKHOUSE_STATEMENT_PREFIX_PATTERN.match(query)
        words = query[prefix.end() if prefix is not None else 0 :].split(None, 1)
        return bool(words) and words[0].upper() in CLICKHOUSE_ROW_RETURNING_KEYWORDS

    def split_script(self, query: str) -> list[str]:
        """The statements of a script, split on the semicolons outside quoted strings, names and
        comments - a statement of comments only is none, which the server refuses as an empty query.

        Args:
            query: The script.

        Returns:
            The statements.
        """
        statements: list[str] = []
        start = 0
        code_parts: list[str] = []
        code_start = 0
        for match in CLICKHOUSE_STATEMENT_END_PATTERN.finditer(query):
            matched_text = match.group(0)
            if matched_text == ";":
                code_parts.append(query[code_start : match.start()])
                if "".join(code_parts).strip():
                    statements.append(query[start : match.start()].strip())
                start = code_start = match.end()
                code_parts = []
            elif matched_text.startswith(CLICKHOUSE_COMMENT_STARTS):
                code_parts.append(query[code_start : match.start()])
                code_start = match.end()
        code_parts.append(query[code_start:])
        if "".join(code_parts).strip():
            statements.append(query[start:].strip())
        return statements

    @staticmethod
    def get_inserted_typed_value(value: Any, column_type: str) -> Any:
        """A value of a ``Dynamic`` or a ``Variant`` column as the driver's binary insert takes it.

        Args:
            value: The value, as the binary insert takes one of its type.
            column_type: Its type, as the server names it.

        Returns:
            The value bound with its type.
        """
        return ClickhouseTypedValue(value, column_type)

    @staticmethod
    def get_error_code(error: BaseException) -> int | None:
        """The ClickHouse error code an error's message names.

        Args:
            error: The error.

        Returns:
            The code, None when the message names none.
        """
        return ClickhouseErrors.get_error_code(error)

    def get_hare_error(self, error: Exception, sql: Any, parameters: Any) -> Exception:
        """hare's exception for a driver's.

        Args:
            error: The driver's exception.
            sql: The statement.
            parameters: Its values.

        Returns:
            The exception to raise.
        """
        return ClickhouseErrors.get_hare_error(self, error, sql, parameters)

    @staticmethod
    def translate_driver_error(
        client: Any,
        error: BaseException,
        sql: str | None,
        parameters: Any,
        args: tuple[Any, ...],
        is_query_executing: bool,
    ) -> BaseException:
        return ClickhouseErrors.translate_driver_error(client, error, sql, parameters, args, is_query_executing)
