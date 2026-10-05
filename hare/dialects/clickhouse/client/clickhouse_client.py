from __future__ import annotations

import abc
import asyncio
import contextlib
import datetime
import ipaddress
import json
import uuid
from collections.abc import AsyncGenerator, Callable, Coroutine, Mapping, Sequence
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast

from hare.classes.value_descriptions import ValueDescriptions
from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.client.declarations import ShellCommand
from hare.dialects.base.connection.connection_options import ConnectionOptions
from hare.dialects.base.literals.constants import SQL_NULL_BYTE, SQL_NULL_BYTE_VALUE_MESSAGE
from hare.dialects.clickhouse.client.clickhouse_connection import ClickhouseConnection
from hare.dialects.clickhouse.client.clickhouse_error_codes import ClickhouseErrorCodes
from hare.dialects.clickhouse.client.clickhouse_external_sets import ClickhouseExternalSets
from hare.dialects.clickhouse.client.clickhouse_query_templates import ClickhouseQueryTemplates
from hare.dialects.clickhouse.client.constants import (
    CLICKHOUSE_AUTHENTICATION_ERROR_CODES,
    CLICKHOUSE_AUTHENTICATION_FAILED_MESSAGE,
    CLICKHOUSE_BAD_TTL_EXPRESSION_ERROR_CODE,
    CLICKHOUSE_CANNOT_CONVERT_TYPE_ERROR_CODE,
    CLICKHOUSE_CLIENT_BASE_SETTINGS,
    CLICKHOUSE_CLOSED_WHILE_OPENING_MESSAGE,
    CLICKHOUSE_CONNECTION_ERROR_CODES,
    CLICKHOUSE_CONNECTION_OPTION_DEFAULTS,
    CLICKHOUSE_DEFAULT_DATABASE,
    CLICKHOUSE_DISCARDED_CLIENT_MESSAGE,
    CLICKHOUSE_FAILED_MUTATIONS_PATTERN,
    CLICKHOUSE_INSERTED_AS_IS_TYPES,
    CLICKHOUSE_INSERTED_MOMENT_COLUMN_TYPES,
    CLICKHOUSE_INSERTED_TEXT_COLUMN_TYPES,
    CLICKHOUSE_INSERTED_UNCHANGED_VALUE_TYPES,
    CLICKHOUSE_INTEGRITY_ERROR_CODES,
    CLICKHOUSE_JOINED_CORRELATED_COLUMN_HINT,
    CLICKHOUSE_JOINED_CORRELATED_COLUMN_MESSAGE,
    CLICKHOUSE_KEEPER_CONNECTIONS_SQL,
    CLICKHOUSE_KEY_SERIES_GAP_SQL,
    CLICKHOUSE_KEY_SERIES_SKIP_SQL,
    CLICKHOUSE_KEY_SERIES_TAKE_SQL,
    CLICKHOUSE_KILL_MUTATIONS_TEMPLATE,
    CLICKHOUSE_MAX_EXECUTION_TIME_SETTING,
    CLICKHOUSE_MISSING_DATABASE_MESSAGE,
    CLICKHOUSE_NOT_IMPLEMENTED_ERROR_CODE,
    CLICKHOUSE_NULL_INTO_NOT_NULL_MESSAGE,
    CLICKHOUSE_NULLABLE_TTL_EXPRESSION_HINT,
    CLICKHOUSE_NULLABLE_TYPE_MARK,
    CLICKHOUSE_ROW_RETURNING_KEYWORDS,
    CLICKHOUSE_ROW_WRITE_PATTERN,
    CLICKHOUSE_SERVER_FACTS_SQL,
    CLICKHOUSE_SHELL_PASSWORD_VARIABLE,
    CLICKHOUSE_SHELL_PROGRAM,
    CLICKHOUSE_STATEMENT_PREFIX_PATTERN,
    CLICKHOUSE_STREAM_BATCH_SIZE,
    CLICKHOUSE_UNKNOWN_DATABASE_ERROR_CODE,
    CLICKHOUSE_UNKNOWN_DATABASE_TEXT,
)
from hare.dialects.clickhouse.client.declarations import (
    ClickhouseDriverErrors,
    ClickhouseExternalSet,
    ClickhouseValueSet,
)
from hare.dialects.clickhouse.cluster.clickhouse_cluster import ClickhouseCluster
from hare.dialects.clickhouse.cluster.clickhouse_cluster_statements import ClickhouseClusterStatements
from hare.dialects.clickhouse.cluster.constants import (
    CLICKHOUSE_CLUSTER_SESSION_SETTINGS,
    CLICKHOUSE_CLUSTER_STATEMENT_PATTERN,
)
from hare.dialects.clickhouse.constants import (
    CLICKHOUSE_COMMENT_STARTS,
    CLICKHOUSE_CONNECTION_OPTIONS,
    CLICKHOUSE_DEFAULT_HTTP_PORT,
    CLICKHOUSE_DIALECT,
    CLICKHOUSE_STATEMENT_END_PATTERN,
    CLICKHOUSE_VALUE_OF_NO_TYPE_MESSAGE,
)
from hare.dialects.clickhouse.drivers.constants import (
    CLICKHOUSE_SESSION_SETTINGS,
    CLICKHOUSE_UNKNOWN_DATABASE_ERROR_CODES,
)
from hare.dialects.clickhouse.introspection.constants import CLICKHOUSE_CURRENT_DATABASE_SQL_FUNCTION
from hare.dialects.clickhouse.keeper.clickhouse_migration_lock import ClickhouseMigrationLock
from hare.dialects.clickhouse.literals.clickhouse_literals import ClickhouseLiterals
from hare.dialects.clickhouse.query.declarations import ClickhouseQuery
from hare.dialects.clickhouse.types.declarations import ClickhouseTypedValue
from hare.exceptions import (
    ConfigurationError,
    DBConnectionError,
    HareError,
    IntegrityError,
    UnSupportedError,
    ValidationError,
)
from hare.fields.data.containers.declarations import MapValue, TupleValue

if TYPE_CHECKING:
    from hare.dialects.base.client.transaction_client import TransactionClient
    from hare.dialects.clickhouse.schema.declarations import ClickhouseSchemaEditor
    from hare.models import Model

ResultType = TypeVar("ResultType")
CoroutineFunction = Callable[..., Coroutine[Any, Any, ResultType]]


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
                await self.close()
                raise ConfigurationError(
                    f"{self.connection_alias!r} asks for transactions (transactions=true), which the server doesn't "
                    f"run - it needs a ClickHouse Keeper and allow_experimental_transactions: {error}"
                ) from error
            # The transactions have no savepoints - a nested atomic() joins the one it is nested in.
            self.features = self.features.replace(supports_transactions=True, supports_savepoints=False)

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
        user: str = "default",
        password: str = "",  # nosec B107 - no password, as ClickHouse's default user
        database: str | None = None,
        **kwargs: Any,
    ) -> None:
        settings = {name: kwargs.pop(name) for name in list(kwargs) if name not in CLICKHOUSE_CLIENT_BASE_SETTINGS}
        super().__init__(**kwargs)
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
        self.user = user
        self.password = password
        self.database = database
        #: The checked connection settings, by name.
        self.options: dict[str, Any] = self.connection_options.read(settings, self.connection_option_defaults)
        self.connection_options.raise_for_unknown(settings, self.driver_name)
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
            await self.run_command_without_database(
                self.get_database_statement(
                    f"CREATE DATABASE IF NOT EXISTS {self.dialect.literals.quote_identifier(self.database or '')}"
                )
            )
        finally:
            await self.close()

    async def db_delete(self) -> None:
        await self.close()
        await self.create_connection(with_db=False)
        try:
            await self.run_command_without_database(
                self.get_database_statement(
                    f"DROP DATABASE IF EXISTS {self.dialect.literals.quote_identifier(self.database or '')}"
                )
            )
        except self.driver_errors.server as error:
            if self.get_error_code(error) not in CLICKHOUSE_UNKNOWN_DATABASE_ERROR_CODES:
                raise
        finally:
            await self.close()

    def get_database_statement(self, statement: str) -> str:
        """A statement creating or dropping the connection's database - on every server of its
        cluster.

        Args:
            statement: The statement.

        Returns:
            The statement to send.
        """
        return statement if self.cluster is None else self.cluster.get_database_statement(statement)

    async def execute_built_script(self, query: str) -> None:
        """Executes a script hare built - over the connection's cluster, where it belongs to one: each
        statement ``ON CLUSTER``, a distributed table's rows changed in its local table.

        Args:
            query: The script.
        """
        if self.cluster is None:
            await self.execute_script(query)
            return
        try:
            await self.execute_script(";\n".join(await self.cluster.get_statements(self.split_script(query))))
        finally:
            # The script may create and drop distributed tables.
            self.cluster.forget_tables()

    async def kill_failed_mutations(
        self, write_sql: str, error: Exception, run_command: Callable[[str], Coroutine[Any, Any, Any]]
    ) -> None:
        """Stops the mutations a failed write names - the server keeps retrying a failed mutation, and
        every later mutation of the table fails behind it.

        Args:
            write_sql: The write.
            error: What the driver raised.
            run_command: Runs a statement on the connection the write ran on.
        """
        mutation_match = CLICKHOUSE_FAILED_MUTATIONS_PATTERN.search(str(error))
        statement_match = CLICKHOUSE_CLUSTER_STATEMENT_PATTERN.fullmatch(write_sql)
        if mutation_match is None or statement_match is None:
            return
        literals = self.dialect.literals
        database_name, table_name = ClickhouseClusterStatements.get_table_key(statement_match["name"])
        ids_sql = ", ".join(
            literals.get_string_literal_sql(mutation_id.strip()) for mutation_id in mutation_match.group(1).split(",")
        )
        on_cluster_sql = "" if self.cluster is None else f" ON CLUSTER {self.cluster.name}"
        try:
            await run_command(
                CLICKHOUSE_KILL_MUTATIONS_TEMPLATE.format(
                    on_cluster=on_cluster_sql,
                    database=CLICKHOUSE_CURRENT_DATABASE_SQL_FUNCTION
                    if database_name is None
                    else literals.get_string_literal_sql(database_name),
                    table=literals.get_string_literal_sql(table_name),
                    ids=ids_sql,
                )
            )
        except self.driver_errors.driver:
            # The write's own error is the one raised - a mutation already gone is no error of it.
            pass

    async def get_row_write_sql(self, sql: str) -> str | None:
        """A write of stored rows - an UPDATE or a DELETE - as the connection runs it: on a cluster, the
        rows of a distributed table changed in its local table on every server.

        Args:
            sql: The statement, inlined.

        Returns:
            The write; None for any other statement.
        """
        if CLICKHOUSE_ROW_WRITE_PATTERN.match(sql) is None:
            return None
        if self.cluster is not None:
            return await self.cluster.get_write_sql(sql)
        return sql

    async def _driver_stream_batches(
        self, query: str, values: list[Any] | None = None, chunk_size: int = 0
    ) -> AsyncGenerator[list[Any]]:
        """The rows of a query as the server sends them, a batch at a time - outside a transaction
        too: the server computes the rows while they are read, no cursor holds them."""
        sql = self.get_inlined_query(query, values)
        try:
            async with contextlib.aclosing(
                self.read_batches(sql, chunk_size or CLICKHOUSE_STREAM_BATCH_SIZE)
            ) as batches:
                async for batch in batches:
                    yield batch
        except self.driver_errors.driver as error:
            raise self.get_hare_error(error, sql, values) from error

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
        table_creation = cast("ClickhouseSchemaEditor", self.dialect.schema_editor_class(self)).table_creation
        for model in table_creation.get_models_to_create():
            await table_creation.raise_if_replicas_keep_rows_apart(model)
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
                if await self.open_across_closes():
                    # A close ran while it opened - one that found nothing to close yet leaves it here.
                    connection, self._connection = cast("Any", self._connection), None
                    if connection is not None:
                        await connection.close()
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
        return cast("int | None", self.options["native_port"])

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
            return await cast("ClickhouseClient", successor).take_statement_connection()

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
        await self.synchronize_key_series(model, checked_once=True)
        series_sql = self.dialect.literals.get_string_literal_sql(self.get_key_series_name(model))
        rows = await self.execute(
            CLICKHOUSE_KEY_SERIES_TAKE_SQL.format(series=series_sql, count=count),
            returns_rows=True,
            rows_by_position=True,
        )
        return [row[0] for row in rows.rows]

    def get_key_series_name(self, model: type[Model]) -> str:
        """The name of the series of a model's keys - the Keeper keeps one series per name for every
        database of the server, so the database is part of it.

        Args:
            model: The model.

        Returns:
            ``database.app.Model``.
        """
        meta = model._meta
        return f"{meta.schema or self.database or CLICKHOUSE_DEFAULT_DATABASE}.{meta.full_name}"

    async def synchronize_key_series(self, model: type[Model], *, checked_once: bool = False) -> None:
        """Moves the model's series past the greatest key of its table - the series hands out the keys
        after it.

        Args:
            model: The model.
            checked_once: Whether a series this process already moved is left alone.
        """
        meta = model._meta
        root_client = (
            cast("TransactionClient", self).get_non_transactional_client() if self.is_transaction_client else self
        )
        synchronized_series = cast("ClickhouseClient", root_client).synchronized_key_series
        series_name = self.get_key_series_name(model)
        if checked_once and series_name in synchronized_series:
            return
        literals = self.dialect.literals
        rows = await self.execute(
            CLICKHOUSE_KEY_SERIES_GAP_SQL.format(
                series=literals.get_string_literal_sql(series_name),
                key=literals.quote_identifier(meta.fields_db_projection[meta.primary_key_attribute_names[0]]),
                table=literals.qualify_table_name(meta.db_table, meta.schema),
            ),
            returns_rows=True,
            rows_by_position=True,
        )
        gap = int(rows.rows[0][0])
        if gap > 0:
            # A key taken is never handed out again: the series moves by taking the keys it lags by.
            series_sql = literals.get_string_literal_sql(series_name)
            await self.execute(CLICKHOUSE_KEY_SERIES_SKIP_SQL.format(series=series_sql, count=gap), returns_rows=True)
        synchronized_series.add(series_name)

    def hold_migration_lock(self) -> contextlib.AbstractAsyncContextManager[None]:
        # Without keeper_hosts no lock is held: the server has no lock given back when a process dies.
        if not self.options["keeper_hosts"]:
            return contextlib.nullcontext()
        return self.hold_keeper_migration_lock()

    @contextlib.asynccontextmanager
    async def hold_keeper_migration_lock(self) -> AsyncGenerator[None]:
        """Holds the database's migration lock in the ClickHouse Keeper of ``keeper_hosts``, waiting while
        another ``migrate`` holds it."""
        migration_lock = ClickhouseMigrationLock(
            ClickhouseMigrationLock.get_addresses(self.options["keeper_hosts"]),
            self.database or CLICKHOUSE_DEFAULT_DATABASE,
            self.options["connect_timeout"],
        )
        try:
            await migration_lock.take()
            yield
        finally:
            await migration_lock.close()

    async def check_keeper_session(self) -> None:
        """Checks that a Keeper server of ``keeper_hosts`` opens a session.

        Raises:
            ConfigurationError: None did, or the setting is not a list of addresses.
        """
        migration_lock = ClickhouseMigrationLock(
            ClickhouseMigrationLock.get_addresses(self.options["keeper_hosts"]),
            self.database or CLICKHOUSE_DEFAULT_DATABASE,
            self.options["connect_timeout"],
        )
        try:
            await migration_lock.check()
        except DBConnectionError as error:
            await self.close()
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

    @classmethod
    def _get_inserted_columns(cls, records: list[tuple[Any, ...]]) -> list[Sequence[Any]]:
        """The records turned into one sequence of values per column, each value as a driver's
        binary insert takes it.

        Args:
            records: One tuple of values per row.

        Returns:
            The columns, in the order of a record's values.

        Raises:
            ValidationError: A string holds a null byte, as where it is written into SQL text.
        """
        inserted_columns: list[Sequence[Any]] = []
        for column in zip(*records, strict=True):
            value_types = set(map(type, column))
            if value_types <= CLICKHOUSE_INSERTED_AS_IS_TYPES:
                if datetime.date in value_types:
                    cls._check_date_column_range(column)
                inserted_columns.append(column)
            elif value_types <= CLICKHOUSE_INSERTED_TEXT_COLUMN_TYPES and SQL_NULL_BYTE not in "".join(
                filter(None, column)
            ):
                inserted_columns.append(column)
            elif value_types <= CLICKHOUSE_INSERTED_MOMENT_COLUMN_TYPES:
                # A naive moment is its UTC wall clock - a driver would read it as local time.
                moments = [
                    value if value is None or value.tzinfo is not None else value.replace(tzinfo=datetime.UTC)
                    for value in column
                ]
                cls._check_moment_column_range(moments)
                inserted_columns.append(moments)
            else:
                inserted_values = [cls._get_inserted_value(value) for value in column]
                if bytes in value_types or bytearray in value_types or memoryview in value_types:
                    # A driver encodes a column of text or takes one of bytes, not a mix.
                    inserted_values = [
                        value.encode("utf-8") if isinstance(value, str) else value for value in inserted_values
                    ]
                inserted_columns.append(inserted_values)
        return inserted_columns

    @classmethod
    def _check_date_column_range(cls, column: Sequence[Any]) -> None:
        """Refuses a column of dates holding one ClickHouse doesn't store - only its earliest
        and its latest date are checked.

        Args:
            column: Dates and None.

        Raises:
            ValidationError: A date is outside 1900-01-01 to 2299-12-31.
        """
        dates = [value for value in column if value is not None]
        if dates:
            ClickhouseLiterals.check_date_range(min(dates))
            ClickhouseLiterals.check_date_range(max(dates))

    @classmethod
    def _check_moment_column_range(cls, column: Sequence[Any]) -> None:
        """Refuses a column of moments holding one ClickHouse doesn't store - only its earliest
        and its latest moment are checked.

        Args:
            column: Aware moments and None.

        Raises:
            ValidationError: A moment is outside 1900-01-01 to 2299-12-31 in UTC.
        """
        moments = [value for value in column if value is not None]
        if moments:
            ClickhouseLiterals.check_moment_range(min(moments))
            ClickhouseLiterals.check_moment_range(max(moments))

    @classmethod
    def _get_inserted_value(cls, value: Any) -> Any:
        """A value as a driver's binary insert takes it - the value its literal
        (``ClickhouseLiterals.get_literal_sql()``) writes into SQL text.

        Args:
            value: The value, ready for the database.

        Returns:
            The value to insert.

        Raises:
            ValidationError: A string holds a null byte, or a date or a moment is one ClickHouse
                doesn't store.
        """
        value_class = value.__class__
        if value_class in CLICKHOUSE_INSERTED_UNCHANGED_VALUE_TYPES:
            return value
        if value_class is ClickhouseTypedValue:
            return cls._get_inserted_typed_value(value)
        while isinstance(value, Enum):
            value = value.value
        if value is None or isinstance(
            value, bool | int | float | Decimal | uuid.UUID | ipaddress.IPv4Address | ipaddress.IPv6Address
        ):
            return value
        if isinstance(value, str):
            if SQL_NULL_BYTE in value:
                raise ValidationError(SQL_NULL_BYTE_VALUE_MESSAGE)
            return value
        if isinstance(value, datetime.datetime):
            moment = value if value.tzinfo is not None else value.replace(tzinfo=datetime.UTC)
            ClickhouseLiterals.check_moment_range(moment)
            return moment
        if isinstance(value, datetime.date):
            ClickhouseLiterals.check_date_range(value)
            return value
        if isinstance(value, datetime.time):
            return value.isoformat()
        if isinstance(value, datetime.timedelta):
            return cls.dialect.literals.get_duration_text(value)
        if isinstance(value, bytes | bytearray | memoryview):
            return bytes(value)
        return cls._get_inserted_composite_value(value)

    @classmethod
    def _get_inserted_typed_value(cls, value: ClickhouseTypedValue) -> Any:
        """A value bound with its ClickHouse type as a driver's binary insert takes it.

        Args:
            value: The typed value.

        Returns:
            The value to insert - with its type when a ``Dynamic`` or ``Variant`` column holds it.
        """
        if value.value is None:
            return None
        inserted_value = cls._get_inserted_value(value.value)
        if value.held_type is None:
            return inserted_value
        return cls.get_inserted_typed_value(inserted_value, value.column_type)

    @classmethod
    def _get_inserted_composite_value(cls, value: Any) -> Any:
        """A value made of other values - or of a class no rule is known for - as a driver's binary
        insert takes it.

        Args:
            value: The value.

        Returns:
            The value to insert: each value inside as it is inserted, a dict as the text of its
            JSON.

        Raises:
            ValidationError: The value is of a class no ClickHouse type holds.
        """
        if isinstance(value, ClickhouseTypedValue):
            return cls._get_inserted_typed_value(value)
        if isinstance(value, TupleValue):
            return tuple(cls._get_inserted_value(element) for element in value)
        if isinstance(value, MapValue):
            return {cls._get_inserted_value(key): cls._get_inserted_value(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [cls._get_inserted_value(element) for element in value]
        if isinstance(value, dict):
            return json.dumps(value, separators=(",", ":"))
        raise ValidationError(
            CLICKHOUSE_VALUE_OF_NO_TYPE_MESSAGE.format(
                value=ValueDescriptions.get_shown_value(value), class_name=type(value).__name__
            )
        )

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
        return ClickhouseErrorCodes.get_error_code(str(error))

    def get_hare_error(self, error: Exception, sql: Any, parameters: Any) -> Exception:
        """hare's exception for a driver's.

        Args:
            error: The driver's exception.
            sql: The statement.
            parameters: Its values.

        Returns:
            The exception to raise.
        """
        code = self.get_error_code(error)
        # An error the server reported its code of is the server's, though a driver reports one in the
        # middle of a stream as a failed connection.
        if code is None and isinstance(error, self.driver_errors.connection):
            return DBConnectionError(error, sql=sql, parameters=parameters)
        if code in CLICKHOUSE_INTEGRITY_ERROR_CODES or (
            code == CLICKHOUSE_CANNOT_CONVERT_TYPE_ERROR_CODE and CLICKHOUSE_NULL_INTO_NOT_NULL_MESSAGE in str(error)
        ):
            # A NULL written into a NOT NULL column is the IntegrityError it is on every database.
            return IntegrityError(error, sql=sql, parameters=parameters)
        if code == CLICKHOUSE_BAD_TTL_EXPRESSION_ERROR_CODE and CLICKHOUSE_NULLABLE_TYPE_MARK in str(error):
            return UnSupportedError(f"{error} - {CLICKHOUSE_NULLABLE_TTL_EXPRESSION_HINT}")
        if code == CLICKHOUSE_NOT_IMPLEMENTED_ERROR_CODE:
            # A statement the server can't run yet - a correlated subquery of a shape it doesn't take.
            if CLICKHOUSE_JOINED_CORRELATED_COLUMN_MESSAGE in str(error):
                return UnSupportedError(f"{error} - {CLICKHOUSE_JOINED_CORRELATED_COLUMN_HINT}")
            return UnSupportedError(f"{error} (sql={sql!r})")
        if code in CLICKHOUSE_CONNECTION_ERROR_CODES:
            return DBConnectionError(error, sql=sql, parameters=parameters)
        if code in CLICKHOUSE_AUTHENTICATION_ERROR_CODES:
            return ConfigurationError(CLICKHOUSE_AUTHENTICATION_FAILED_MESSAGE.format(user=self.user, error=error))
        if (
            code == CLICKHOUSE_UNKNOWN_DATABASE_ERROR_CODE
            and self.database
            and CLICKHOUSE_UNKNOWN_DATABASE_TEXT.format(database=self.database) in str(error)
        ):
            return ConfigurationError(CLICKHOUSE_MISSING_DATABASE_MESSAGE.format(database=self.database, error=error))
        return self._get_operational_error(error, sql=sql, parameters=parameters)

    @staticmethod
    def translate_driver_error(
        client: Any,
        error: BaseException,
        sql: str | None,
        parameters: Any,
        args: tuple[Any, ...],
        is_query_executing: bool,
    ) -> BaseException:
        if isinstance(error, HareError) or not isinstance(error, client.driver_errors.driver):
            return error
        return client.get_hare_error(error, sql, parameters)
