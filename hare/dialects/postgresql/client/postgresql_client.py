from __future__ import annotations

import abc
import asyncio
import time
import uuid
from asyncio.events import AbstractEventLoop
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, ClassVar, Self, SupportsInt, TypeVar, cast

from hare.core.caching.cache import Cache
from hare.dialects.base.client.connect_failure_reports import ConnectFailureReports
from hare.dialects.base.client.connection_wrapper import ConnectionWrapper
from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.client.declarations import ShellCommand
from hare.dialects.base.client.password_provider import PasswordProvider
from hare.dialects.base.client.pool.pool_connection_wrapper import PoolConnectionWrapper
from hare.dialects.base.literals.constants import SQL_NULL_BYTE
from hare.dialects.base.results.statement_result import StatementResult
from hare.dialects.postgresql.client.constants import (
    CTE_DEFINITION_RE,
    CTE_INTRODUCER_RE,
    LEADING_WORD_RE,
    PARENTHESIZED_GROUP_RE,
    POSTGRES_AUTHORIZATION_SQLSTATE_CLASS,
    POSTGRES_CREATE_LEGACY_PUBLIC_SCHEMA_STATEMENTS,
    POSTGRES_CREATE_PUBLIC_SCHEMA_STATEMENTS,
    POSTGRES_DATABASE_EXISTS_SQL,
    POSTGRES_DATABASE_OWNER_PUBLIC_SCHEMA_VERSION_NUMBER,
    POSTGRES_DATABASE_PREPARED_TRANSACTIONS_SQL,
    POSTGRES_DATABASE_ROLE_SETTINGS_SQL,
    POSTGRES_DROP_DATABASE_SQL,
    POSTGRES_DROP_SCHEMA_CASCADE_SQL,
    POSTGRES_FORCED_DROP_DATABASE_SQL,
    POSTGRES_PASSWORD_ENVIRONMENT_VARIABLE,
    POSTGRES_RESET_DATABASE_ROLE_SETTINGS_SQL,
    POSTGRES_RESET_DATABASE_SETTINGS_SQL,
    POSTGRES_ROLLBACK_PREPARED_SQL,
    POSTGRES_SERVER_VERSION_NUMBER_MAJOR_FACTOR,
    POSTGRES_SESSION_TIME_ZONE,
    POSTGRES_SESSION_TIME_ZONE_SETTING,
    POSTGRES_SHELL_APPLICATION_NAME_VARIABLE,
    POSTGRES_SHELL_OPTIONS_VARIABLE,
    POSTGRES_SHELL_PROGRAM,
    POSTGRES_STATEMENT_NULL_BYTE_MESSAGE,
    POSTGRES_TERMINATE_OTHER_DATABASE_SESSIONS_SQL,
    POSTGRES_USER_SCHEMAS_SQL,
    POSTGRES_UTC_TIME_ZONE_NAMES,
    POSTGRESQL_POOLER_LOGIN_REFUSAL_SQLSTATE,
    POSTGRESQL_POOLER_LOGIN_REFUSAL_TEXT,
    POSTGRESQL_SINGLE_CONNECTION_POOL_SETTINGS,
    RETURNING_CLAUSE_RE,
    SQL_BLOCK_COMMENT_DELIMITER_RE,
    SQL_COMMENT_OR_QUOTED_TEXT_RE,
    SQL_QUOTED_TEXT_PLACEHOLDER,
    WRITE_STATEMENT_KEYWORDS,
    WRITE_STATEMENT_TYPE_CACHED_TEXT_MAX_LENGTH,
)
from hare.dialects.postgresql.client.transaction_pooler import TransactionPooler
from hare.dialects.postgresql.constants import (
    POSTGRES_CONNECTION_OPTION_DEFAULTS,
    POSTGRES_CONNECTION_OPTIONS,
    POSTGRES_DEFAULT_PORT,
    POSTGRES_PORT_OPTION,
    POSTGRES_SERVER_VERSION_NUMBER_SQL,
    POSTGRESQL_DEFAULT_SCHEMA,
    POSTGRESQL_DIALECT,
    REUSABLE_TEST_DATABASE_LEASE_CREDENTIAL,
)
from hare.dialects.postgresql.query.declarations import PostgresqlQuery
from hare.exceptions import (
    ConfigurationError,
    DatabaseError,
    DBConnectionError,
    ValidationError,
)
from hare.instrumentation.observers.observers import Observers
from hare.instrumentation.pools.pool_registry import PoolRegistry
from hare.sql.builder.queries.query import Query

if TYPE_CHECKING:
    from asyncpg.connection import Connection

T = TypeVar("T")


CoroutineFunction = Callable[..., Coroutine[None, None, T]]


class PostgresqlClient(DatabaseClient, abc.ABC):
    rejected_sql_character: ClassVar[str | None] = SQL_NULL_BYTE
    runs_statement_options: ClassVar[bool] = True
    stale_statement_runner: ClassVar[Callable[..., Any] | None] = TransactionPooler.run_replanning_stale_statements

    def raise_rejected_sql_character(self, sql: str) -> None:
        PostgresqlClient.raise_if_statement_has_null_byte(sql)

    @staticmethod
    def translate_driver_error(
        client: Any,
        error: BaseException,
        sql: str | None,
        parameters: Any,
        args: tuple[Any, ...],
        is_query_executing: bool,
    ) -> BaseException:
        translated = error
        if isinstance(error, Exception):
            translated = client.get_driver_error(error, args) or error
        if isinstance(translated, DatabaseError) and translated.sql is None:
            translated.sql = sql
            translated.parameters = parameters
        if client.is_transaction_client:
            if is_query_executing and isinstance(translated, asyncio.CancelledError):
                client._mark_statement_interrupted()
            else:
                client._mark_statement_failed()
        return translated

    query_class: type[Query] = PostgresqlQuery
    #: Statement text -> whether it is a write reporting a row count (_is_write_without_returning()).
    write_statement_types: ClassVar[Cache[bool]] = Cache(
        Cache.max_size_from_env(), holds_sql=False, keyed_by_model=False
    )
    native_python_types = DatabaseClient.native_python_types | {bool, uuid.UUID}

    @staticmethod
    async def run_renewing_refused_password(
        method: CoroutineFunction[Any], client: PostgresqlClient, *args: Any, **kwargs: Any
    ) -> Any:
        """Runs ``method``, once more with a new password when the server refused the one a new pool
        connection was opened with - a rotated credential; the statement never reached the server.

        Args:
            method: The client method.
            client: The client.
            args: The method's positional arguments.
            kwargs: The method's keyword arguments.

        Returns:
            What the method returned.
        """
        try:
            return await method(client, *args, **kwargs)
        except Exception as error:
            if not client.is_authentication_failure(error):
                raise
        await client.renew_password()
        return await method(client, *args, **kwargs)

    @staticmethod
    def raise_if_statement_has_null_byte(sql: str) -> None:
        """Rejects a statement text holding a null byte, which the Postgres protocol can't carry.

        Args:
            sql: The statement text.

        Raises:
            ValidationError: The text holds a null byte.
        """
        if SQL_NULL_BYTE in sql:
            raise ValidationError(POSTGRES_STATEMENT_NULL_BYTE_MESSAGE)

    dialect = POSTGRESQL_DIALECT
    features = POSTGRESQL_DIALECT.features.replace(supports_positional_rows=True, supports_pool_status=True)
    connection_class: Connection | None = None
    loop: AbstractEventLoop | None = None
    # A transaction's client works on the connection its transaction holds - none of these apply.
    direct_host: str | None = None
    direct_port: int | None = None
    _pool: Any = None
    _connection: Any = None
    #: The exceptions a failed connect is retried on - set per driver; none by default.
    RETRYABLE_CONNECT_EXCEPTIONS: tuple[type[BaseException], ...] = ()

    def __init__(
        self,
        user: str | None = None,
        password: str | None = None,
        database: str | None = None,
        host: str | None = None,
        port: SupportsInt = POSTGRES_DEFAULT_PORT,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)

        self.user = user
        self.password = password
        self.database = database
        self.host = host
        self.port = POSTGRES_PORT_OPTION.parse(port)
        self.extra = kwargs.copy()
        self.server_settings = self._get_server_settings_with_utc_session(self.extra.pop("server_settings", None))
        self.extra.pop("connection_alias", None)
        self.extra.pop("fetch_inserted", None)
        self.reusable_test_database_lease: int | None = self.extra.pop(REUSABLE_TEST_DATABASE_LEASE_CREDENTIAL, None)
        self.loop = self.extra.pop("loop", None)
        self.connection_class = self.extra.pop("connection_class", self.connection_class)
        settings = POSTGRES_CONNECTION_OPTIONS.read(self.extra, POSTGRES_CONNECTION_OPTION_DEFAULTS)
        self.schema = settings["schema"]
        self.application_name = settings["application_name"]
        self.password_provider = PasswordProvider.from_settings(settings, self.password)
        if settings["tenant_schema_template"] is not None:
            # Local import: the tenant schemas import the models, which import the dialects.
            from hare.models.tenancy.tenant_schemas import TenantSchemas

            self.tenant_schema_template = TenantSchemas.get_checked_template(settings["tenant_schema_template"])
        self.tenant_row_level_security: bool = settings["tenant_row_level_security"]
        #: Whether the connection goes through a pooler in transaction pooling (PgBouncer, RDS Proxy) -
        #: a server connection serves one transaction at a time, so what needs a session of its own
        #: (LISTEN, a session setting) opens one on the server itself; a database it drops loses
        #: the sessions the pooler keeps on it too.
        self.transaction_pooling: bool = settings["transaction_pooling"]
        #: The address of the server behind the pooler - None for no direct way to it.
        self.direct_host: str | None = settings["direct_host"]
        self.direct_port: int | None = settings["direct_port"]
        if (self.direct_host is not None or self.direct_port is not None) and not self.transaction_pooling:
            raise ConfigurationError(
                "direct_host/direct_port name the server behind a transaction pooler - set "
                "transaction_pooling=true for a connection going through one"
            )
        if self.direct_port is not None and self.direct_host is None:
            raise ConfigurationError("direct_port needs direct_host - the server behind the pooler")
        self.pool_minsize: int = settings["min_size"]
        self.pool_maxsize: int = settings["max_size"]
        if self.pool_minsize > self.pool_maxsize:
            raise ConfigurationError(
                f"min_size ({self.pool_minsize}) must not be greater than max_size ({self.pool_maxsize})"
            )
        self.connect_max_retries: int = settings["connect_max_retries"]
        self.connect_retry_backoff_base_seconds: float = settings["connect_retry_backoff_base_seconds"]
        self.read_retry_max_retries: int = settings["read_retry_max_retries"]
        self.read_retry_backoff_base_seconds: float = settings["read_retry_backoff_base_seconds"]
        if "statement_cache_size" in settings:
            self.extra["statement_cache_size"] = settings["statement_cache_size"]
        self.pool_acquire_timeout: float | None = settings["pool_acquire_timeout"]
        self.command_timeout: float | None = settings["command_timeout"]

        self._template: dict[str, Any] = {}
        self._pool = None
        self._connection = None
        self._pool_init_lock = asyncio.Lock()
        #: The client of the server itself behind the transaction pooler
        #: (``TransactionPooler.get_direct_client()``).
        self._direct_client: Self | None = None

    @staticmethod
    def _get_server_settings_with_utc_session(server_settings: dict[str, Any] | None) -> dict[str, Any]:
        """A copy of the configured ``server_settings`` with the session ``TimeZone`` set to UTC.

        Args:
            server_settings: The configured settings, or None.

        Returns:
            The settings every connection is opened with.

        Raises:
            ConfigurationError: The settings name a ``TimeZone`` other than UTC.
        """
        settings = dict(server_settings or {})
        for key in [key for key in settings if str(key).lower() == POSTGRES_SESSION_TIME_ZONE_SETTING.lower()]:
            configured_zone = settings.pop(key)
            if str(configured_zone).strip().lower() not in POSTGRES_UTC_TIME_ZONE_NAMES:
                raise ConfigurationError(
                    f"server_settings {key}={configured_zone!r} is not supported - hare runs every "
                    f"Postgres session with TimeZone={POSTGRES_SESSION_TIME_ZONE} (date/time casts, "
                    "CURRENT_DATE and db_default=Now() read the session zone). Set the zone values "
                    "are shown in with Hare.init(timezone=...) instead."
                )
        settings[POSTGRES_SESSION_TIME_ZONE_SETTING] = POSTGRES_SESSION_TIME_ZONE
        return settings

    async def _run_with_command_timeout(self, function: CoroutineFunction[Any], *args: Any, **kwargs: Any) -> Any:
        """Runs ``func(self, ...)``, cancelling it once ``command_timeout`` elapses. Enforced here:
        asyncpg's own option waits for its cancel request to be answered, and rust_pg has none.

        Raises:
            TimeoutError: The call outlived ``command_timeout``.
        """
        try:
            async with asyncio.timeout(self.command_timeout) as timeout_scope:
                return await function(self, *args, **kwargs)
        except TimeoutError:
            if not timeout_scope.expired():
                raise
            raise TimeoutError(f"Command timed out after {self.command_timeout}s") from None

    @abc.abstractmethod
    async def create_connection(self, with_db: bool) -> None:
        raise NotImplementedError("create_connection is not implemented")

    @abc.abstractmethod
    async def create_pool(self, **kwargs: Any) -> Any:
        raise NotImplementedError("create_pool is not implemented")

    async def _create_pool_with_retry(self, **kwargs: Any) -> Any:
        """Creates the pool, retrying on the driver's ``RETRYABLE_CONNECT_EXCEPTIONS`` with exponential
        backoff. Off by default (``connect_max_retries=0``).
        """
        attempt = 0
        while True:
            try:
                return await self.create_pool(**kwargs)
            except Exception as error:
                # A refused credential is the same on every attempt.
                will_retry = (
                    isinstance(error, self.RETRYABLE_CONNECT_EXCEPTIONS)
                    and attempt < self.connect_max_retries
                    and not self.is_authentication_failure(error)
                )
                ConnectFailureReports.record(self, error, attempt=attempt + 1, will_retry=will_retry)
                if not will_retry:
                    raise
                delay = self.connect_retry_backoff_base_seconds * (2**attempt)
                self.log.debug(
                    "Connection attempt %d/%d failed (%s), retrying in %.2fs",
                    attempt + 1,
                    self.connect_max_retries,
                    error,
                    delay,
                )
                await asyncio.sleep(delay)
                attempt += 1

    async def _execute_read_query_with_retry(
        self,
        function: CoroutineFunction[Any],
        *args: Any,
        sql: str | None = None,
        parameters: list[Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Runs a read-only query, retrying on ``DBConnectionError`` with exponential backoff - each
        retry takes a fresh pool connection. Never retried: a write, and an ``OperationalError`` (a
        cancelled query leaves the connection usable). Off by default
        (``read_retry_max_retries=0``). Each attempt is reported to the observers separately,
        without the backoff sleep.
        """
        attempt = 0
        while True:
            attempt_start = time.perf_counter()
            try:
                result = await self._translate_exceptions(function, *args, **kwargs)
            except DBConnectionError as error:
                Observers.record_query(sql, parameters, attempt_start, error, self.connection_alias)
                if attempt >= self.read_retry_max_retries:
                    raise
                delay = self.read_retry_backoff_base_seconds * (2**attempt)
                self.log.debug(
                    "Read query attempt %d/%d failed (%s), retrying in %.2fs",
                    attempt + 1,
                    self.read_retry_max_retries,
                    error,
                    delay,
                )
                await asyncio.sleep(delay)
                attempt += 1
            except Exception as error:
                Observers.record_query(sql, parameters, attempt_start, error, self.connection_alias)
                raise
            else:
                Observers.record_query(sql, parameters, attempt_start, None, self.connection_alias)
                return result

    async def _pool_acquire(self) -> Any:
        """Takes one raw connection from the pool - each driver overrides it with its own timeout and
        exception translation.
        """
        return await self._pool.acquire()

    async def _post_connect(self) -> None:
        await super()._post_connect()
        if self.transaction_pooling and "search_path" in self.server_settings:
            await TransactionPooler.check_search_path(self, self.server_settings["search_path"])

    def get_server_client(self) -> PostgresqlClient:
        """The client of the server past the transaction pooler (``direct_host``) - this client for a
        connection going straight to the server.

        Raises:
            ConfigurationError: The connection goes through a pooler and names no direct way to the
                server.
        """
        return TransactionPooler.get_direct_client(self)

    def get_address(self) -> str:
        return f"{self.host}:{self.port}"

    async def get_shell_command(self) -> ShellCommand:
        arguments = [POSTGRES_SHELL_PROGRAM, "--port", str(self.port)]
        if self.host:
            arguments += ["--host", self.host]
        if self.user:
            arguments += ["--username", self.user]
        if self.database:
            arguments += ["--dbname", self.database]
        environment = self.get_shell_ssl_environment()
        password = await self.password_provider.get() if self.password_provider is not None else self.password
        if password:
            environment[POSTGRES_PASSWORD_ENVIRONMENT_VARIABLE] = password
        if self.application_name:
            environment[POSTGRES_SHELL_APPLICATION_NAME_VARIABLE] = self.application_name
        if self.schema:
            environment[POSTGRES_SHELL_OPTIONS_VARIABLE] = f"-c search_path={self.schema}"
        return ShellCommand(tuple(arguments), environment)

    def get_shell_ssl_environment(self) -> dict[str, str]:
        """The libpq environment variables of the connection's TLS settings - for ``psql``.

        Returns:
            The variables, none for the driver's defaults.
        """
        return {}

    async def get_server_version(self) -> tuple[int, ...]:
        server_version_number = await self._fetch_server_version_number()
        return (
            server_version_number // POSTGRES_SERVER_VERSION_NUMBER_MAJOR_FACTOR,
            server_version_number % POSTGRES_SERVER_VERSION_NUMBER_MAJOR_FACTOR,
        )

    @abc.abstractmethod
    async def _fetch_server_version_number(self) -> int:
        """Reads ``server_version_num`` straight off the pool, bypassing query instrumentation.

        Returns:
            The number - ``150004`` for 15.4.
        """
        raise NotImplementedError("_fetch_server_version_number is not implemented")

    @abc.abstractmethod
    async def _expire_connections(self) -> None:
        raise NotImplementedError("_expire_connections is not implemented")

    @abc.abstractmethod
    async def _close(self) -> None:
        raise NotImplementedError("_close is not implemented")

    @abc.abstractmethod
    def get_driver_error(self, error: Exception, call_arguments: tuple[Any, ...]) -> Exception | None:
        """The hare exception a driver's exception is raised as.

        Args:
            error: The exception a client method raised.
            call_arguments: The method's positional arguments (query text, bind values).

        Returns:
            The hare exception, None for an exception raised as it is.
        """
        raise NotImplementedError("get_driver_error is not implemented")

    async def _translate_exceptions(self, function: CoroutineFunction[Any], *args: Any, **kwargs: Any) -> Any:
        """Runs ``func(self, ...)``, raising the driver's exceptions as hare's.

        Args:
            function: The method.
            args: Its positional arguments.
            kwargs: Its keyword arguments.

        Returns:
            What it returned.
        """
        try:
            return await function(self, *args, **kwargs)
        except Exception as driver_error:
            error = self.get_driver_error(driver_error, args)
            if error is None:
                raise
            raise error from driver_error

    async def close(self) -> None:
        PoolRegistry.remove(self)
        direct_client = self._direct_client
        if direct_client is not None:
            self._direct_client = None
            await direct_client.close()
        await self.close_tenant_clients()
        await self._close()
        self._template.clear()

    def get_tenant_client_settings(self, schema_name: str) -> dict[str, Any]:
        """A search path of the tenant's schema, then the connection's own schema (``public``)."""
        return {
            "schema": f"{schema_name},{self.schema or POSTGRESQL_DEFAULT_SCHEMA}",
            "tenant_schema_template": None,
        }

    def is_authentication_failure(self, error: BaseException) -> bool:
        """Whether a refused connection is a bad credential - the server's SQLSTATE class 28, or, past
        a transaction pooler, the pooler's own refusal of the login.

        Args:
            error: The driver's error.

        Returns:
            True for a refused credential - retrying can't fix it.
        """
        sqlstate = str(getattr(error, "sqlstate", None) or "")
        if sqlstate.startswith(POSTGRES_AUTHORIZATION_SQLSTATE_CLASS):
            return True
        return (
            self.transaction_pooling
            and sqlstate == POSTGRESQL_POOLER_LOGIN_REFUSAL_SQLSTATE
            and POSTGRESQL_POOLER_LOGIN_REFUSAL_TEXT in str(error)
        )

    @staticmethod
    @abc.abstractmethod
    def is_missing_database_error(error: BaseException) -> bool:
        """Whether the driver's failure to connect is the configured database missing (SQLSTATE 3D).

        Args:
            error: The driver's error.

        Returns:
            True for a missing database - retrying can't fix it.
        """

    @staticmethod
    def is_invalid_parameter_error(error: BaseException) -> bool:
        """Whether the driver refused a configured connection parameter it doesn't take.

        Args:
            error: The driver's error.

        Returns:
            True for a refused parameter; False for a driver taking every parameter it gets.
        """
        return False

    @staticmethod
    @abc.abstractmethod
    def is_stale_plan_error(error: Exception) -> bool:
        """Whether the driver's error is the server refusing a prepared statement whose result a
        schema change altered.

        Args:
            error: The driver's error.

        Returns:
            True for that refusal.
        """

    @asynccontextmanager
    async def lock_timeout_session(self, seconds: float) -> AsyncGenerator[DatabaseClient]:
        """A client of its own on one connection with ``SET lock_timeout`` - a pooled connection
        would lose the setting to the statement after it; past a transaction pooler, the server's
        own. Closed when the block exits."""
        session_client = self.create_independent_client(
            {**POSTGRESQL_SINGLE_CONNECTION_POOL_SETTINGS, **TransactionPooler.get_direct_settings(self)}
        )
        try:
            session_lock_timeout_sql = self.dialect.transactions.get_session_lock_timeout_sql(
                max(1, round(seconds * 1000))
            )
            await session_client.execute(cast("str", session_lock_timeout_sql))
            yield session_client
        finally:
            await session_client.close()

    async def _reconnect(self, with_db: bool) -> None:
        """Opens a fresh pool for a database-level statement (CREATE/DROP DATABASE), closing the
        current pool first - its sessions would keep the database from being dropped.

        Args:
            with_db: Whether to connect to the configured database - False connects to the server's
                default one.
        """
        await self.close()
        await self.create_connection(with_db)

    async def db_create(self) -> None:
        if not self.database:
            raise ConfigurationError("Can't create a database without a configured database name")
        if self.reusable_test_database_lease is not None:
            await self._prepare_reusable_test_database()
            return
        await self._create_database()

    async def _create_database(self) -> None:
        """Runs CREATE DATABASE for the configured database, owned by the configured user."""
        assert self.database is not None  # nosec B101 - checked by db_create()
        statement = f"CREATE DATABASE {self.dialect.literals.quote_identifier(self.database)}"
        # No configured user means the driver connects as the default (PGUSER/OS) role, which then
        # owns the new database - there is no role name to name here.
        if self.user:
            statement += f" OWNER {self.dialect.literals.quote_identifier(self.user)}"
        await self._reconnect(with_db=False)
        try:
            await self.execute_script(statement)
        finally:
            await self.close()

    async def db_delete(self) -> None:
        if not self.database:
            raise ConfigurationError("Can't drop a database without a configured database name")
        if self.reusable_test_database_lease is not None:
            await self._release_reusable_test_database()
            return
        await self._reconnect(with_db=False)
        drop_sql = POSTGRES_FORCED_DROP_DATABASE_SQL if self.transaction_pooling else POSTGRES_DROP_DATABASE_SQL
        try:
            await self.execute_script(drop_sql.format(database=self.dialect.literals.quote_identifier(self.database)))
        finally:
            await self.close()

    async def _prepare_reusable_test_database(self) -> None:
        """Makes a leased test database exist and be empty: reuses it as is when this process
        already reset it, resets it when it exists from before, creates it otherwise."""
        from hare.contrib.test.databases.reusable_test_databases import ReusableTestDatabases

        assert self.database is not None  # nosec B101 - checked by db_create()
        if not ReusableTestDatabases.is_clean(self.database):
            if await self._database_exists():
                await self._reset_database()
            else:
                await self._create_database()
        ReusableTestDatabases.mark_used(self.database)

    async def _release_reusable_test_database(self) -> None:
        """Resets a leased test database and returns it to the pool - also when the reset fails,
        leaving it marked as needing a reset before its next use."""
        from hare.contrib.test.databases.reusable_test_databases import ReusableTestDatabases

        assert self.database is not None and self.reusable_test_database_lease is not None  # nosec B101
        if not ReusableTestDatabases.is_leased(self.database, self.reusable_test_database_lease):
            return  # already released - the slot may belong to another context by now
        is_clean = False
        try:
            is_clean = await self._reset_database()
        finally:
            ReusableTestDatabases.release(self.database, self.reusable_test_database_lease, is_clean=is_clean)

    async def _database_exists(self) -> bool:
        """Whether the configured database exists on the server.

        Returns:
            True if pg_database has it.
        """
        await self._reconnect(with_db=False)
        try:
            rows = await self.execute_dicts(POSTGRES_DATABASE_EXISTS_SQL, [self.database])
        finally:
            await self.close()
        return bool(rows)

    async def _reset_database(self) -> bool:
        """Brings the configured database back to the state of a freshly created one without
        dropping it: ends its other sessions, rolls back its prepared transactions, drops every
        non-system schema (with the extensions installed in them), recreates the public schema
        and clears database-level settings.

        Returns:
            False when the database does not exist, True once it was reset.
        """
        # Past a transaction pooler the reset runs on a session of the server's own: a pooled session it
        # ran on would stay in the pooler, the dropped extensions' objects cached - PostGIS keeps their ids
        # per session, and a query of a later test failed on the extension created again.
        pooled_address = self.host, self.port
        if self.transaction_pooling and self.direct_host is not None:
            self.host, self.port = self.direct_host, self.direct_port or self.port
        try:
            return await self._reset_database_on_its_session()
        finally:
            self.host, self.port = pooled_address

    async def _reset_database_on_its_session(self) -> bool:
        """``_reset_database()`` on the session the client's address opens.

        Returns:
            False when the database does not exist, True once it was reset.
        """
        try:
            await self._reconnect(with_db=True)
        except ConfigurationError:
            await self.close()
            if not await self._database_exists():
                return False
            raise

        try:
            await self.execute(POSTGRES_TERMINATE_OTHER_DATABASE_SESSIONS_SQL)
            for prepared_transaction_row in await self.execute_dicts(POSTGRES_DATABASE_PREPARED_TRANSACTIONS_SQL):
                gid_literal = self.dialect.literals.get_string_literal_sql(prepared_transaction_row["gid"])
                await self.execute_script(POSTGRES_ROLLBACK_PREPARED_SQL.format(gid_literal=gid_literal))
            schema_rows = await self.execute_dicts(POSTGRES_USER_SCHEMAS_SQL)
            role_setting_rows = await self.execute_dicts(POSTGRES_DATABASE_ROLE_SETTINGS_SQL)
            version_rows = await self.execute_dicts(POSTGRES_SERVER_VERSION_NUMBER_SQL)
            quoted_database = self.dialect.literals.quote_identifier(self.database or "")
            statements = [
                POSTGRES_DROP_SCHEMA_CASCADE_SQL.format(
                    schema=self.dialect.literals.quote_identifier(schema_row["nspname"])
                )
                for schema_row in schema_rows
            ]
            if version_rows[0]["server_version_number"] >= POSTGRES_DATABASE_OWNER_PUBLIC_SCHEMA_VERSION_NUMBER:
                statements.extend(POSTGRES_CREATE_PUBLIC_SCHEMA_STATEMENTS)
            else:
                statements.extend(POSTGRES_CREATE_LEGACY_PUBLIC_SCHEMA_STATEMENTS)
            statements.append(POSTGRES_RESET_DATABASE_SETTINGS_SQL.format(database=quoted_database))
            statements.extend(
                POSTGRES_RESET_DATABASE_ROLE_SETTINGS_SQL.format(
                    role=role_setting_row["role_name"], database=quoted_database
                )
                for role_setting_row in role_setting_rows
            )
            await self.execute_script(";\n".join(statements))
        finally:
            await self.close()
        return True

    def acquire_connection(self) -> ConnectionWrapper[Any] | PoolConnectionWrapper[Any]:
        return PoolConnectionWrapper(self, self._pool_init_lock)

    @staticmethod
    def _strip_comments_and_quoted_text(query: str) -> str:
        """Blanks out comments (nested block comments included) and replaces every string
        literal, quoted identifier and dollar-quoted string with a placeholder word.

        Args:
            query: The SQL text to inspect.

        Returns:
            The query text with only unquoted SQL left readable.
        """
        parts: list[str] = []
        position = 0
        while (token_match := SQL_COMMENT_OR_QUOTED_TEXT_RE.search(query, position)) is not None:
            parts.append(query[position : token_match.start()])
            token = token_match.group()
            if token == "/*":  # nosec B105 - a comment marker, not a password
                depth = 1
                position = len(query)
                for delimiter_match in SQL_BLOCK_COMMENT_DELIMITER_RE.finditer(query, token_match.end()):
                    depth += 1 if delimiter_match.group() == "/*" else -1
                    if depth == 0:
                        position = delimiter_match.end()
                        break
                parts.append(" ")
            elif token.startswith("--"):
                parts.append(" ")
                position = token_match.end()
            else:
                parts.append(SQL_QUOTED_TEXT_PLACEHOLDER)
                position = token_match.end()
        parts.append(query[position:])
        return "".join(parts)

    @staticmethod
    def _get_main_statement_text(query: str) -> str:
        """The query's main statement with comments and quoted text blanked out, every
        parenthesized group removed and any leading ``WITH`` CTE list skipped.

        Args:
            query: The SQL text to inspect.

        Returns:
            The main statement's remaining top-level text.
        """
        remaining = PostgresqlClient._strip_comments_and_quoted_text(query)
        while True:
            reduced = PARENTHESIZED_GROUP_RE.sub(" ", remaining)
            if reduced == remaining:
                break
            remaining = reduced
        cte_introducer_match = CTE_INTRODUCER_RE.match(remaining)
        if cte_introducer_match is None:
            return remaining
        remaining = remaining[cte_introducer_match.end() :]
        while (cte_definition_match := CTE_DEFINITION_RE.match(remaining)) is not None:
            remaining = remaining[cte_definition_match.end() :]
        return remaining

    @staticmethod
    def _is_write_without_returning(query: str) -> bool:
        """Whether `query` is an UPDATE/DELETE/INSERT/MERGE with no top-level RETURNING clause -
        one that reports an affected-row count instead of result rows. Comments, quoted text,
        CTEs (``[NOT] MATERIALIZED`` included) and a RETURNING inside a CTE are all accounted for.
        Worked out once per statement text - a write of one row sends the same text every time.

        Args:
            query: The SQL text to inspect.

        Returns:
            True if the caller should read an affected-row count rather than fetch result rows.
        """
        if len(query) > WRITE_STATEMENT_TYPE_CACHED_TEXT_MAX_LENGTH:
            return PostgresqlClient._reads_write_without_returning(query)
        key = (query,)
        is_write_without_returning = PostgresqlClient.write_statement_types.get(key)
        if is_write_without_returning is None:
            is_write_without_returning = PostgresqlClient._reads_write_without_returning(query)
            PostgresqlClient.write_statement_types[key] = is_write_without_returning
        return is_write_without_returning

    @staticmethod
    def _reads_write_without_returning(query: str) -> bool:
        """``_is_write_without_returning()`` read off the statement text itself.

        Args:
            query: The SQL text to inspect.

        Returns:
            True if the caller should read an affected-row count rather than fetch result rows.
        """
        leading_word_match = LEADING_WORD_RE.match(query)
        leading_word = leading_word_match.group(1).upper() if leading_word_match else ""
        if leading_word in WRITE_STATEMENT_KEYWORDS:
            if RETURNING_CLAUSE_RE.search(query) is None:
                return True
        elif leading_word_match is not None and leading_word != "WITH":
            return False
        main_statement = PostgresqlClient._get_main_statement_text(query)
        main_keyword_match = LEADING_WORD_RE.match(main_statement)
        if main_keyword_match is None or main_keyword_match.group(1).upper() not in WRITE_STATEMENT_KEYWORDS:
            return False
        return RETURNING_CLAUSE_RE.search(main_statement) is None

    async def _ensure_connection(self) -> None:
        """Creates the pool on first use - once, whichever task gets there first."""
        if not self._pool:
            async with self._pool_init_lock:
                if not self._pool:
                    await self.create_connection(with_db=True)

    @abc.abstractmethod
    async def execute_many(self, query: str, values: list[Any]) -> None:
        raise NotImplementedError("execute_many is not implemented")

    @abc.abstractmethod
    async def execute(
        self,
        query: str,
        values: list[Any] | None = None,
        *,
        returns_rows: bool | None = None,
        rows_by_position: bool = False,
    ) -> StatementResult:
        raise NotImplementedError("execute is not implemented")

    async def notify(self, channel: str, payload: str = "") -> None:
        """Sends a Postgres ``NOTIFY`` on ``channel`` via parameterized ``SELECT pg_notify($1, $2)``.

        On a transaction client (inside ``Transactions.atomic()``) the notification is
        queued by Postgres and delivered only if and when that transaction commits - never on
        rollback. Outside a transaction it is delivered immediately.

        Args:
            channel: Channel name (any string - no identifier quoting needed).
            payload: Notification payload, delivered to listeners as-is.
        """
        await self.execute("SELECT pg_notify($1, $2)", [channel, payload])
