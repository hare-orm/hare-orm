from __future__ import annotations

import abc
import asyncio
import ssl
import time
import uuid
from asyncio.events import AbstractEventLoop
from collections.abc import AsyncGenerator, Callable, Coroutine, Mapping
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, ClassVar, Self, SupportsInt, TypeVar, cast

from hare.core.caching.cache import Cache
from hare.dialects.base.client.connect_failure_reports import ConnectFailureReports
from hare.dialects.base.client.connection_wrapper import ConnectionWrapper
from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.client.declarations import ShellCommand
from hare.dialects.base.client.password_provider import PasswordProvider
from hare.dialects.base.client.pool.pool_connection_wrapper import PoolConnectionWrapper
from hare.dialects.base.literals.constants import SQL_LONG_INT_MESSAGE, SQL_NULL_BYTE
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
    POSTGRES_FAILING_ROW_RE,
    POSTGRES_FORCED_DROP_DATABASE_SQL,
    POSTGRES_HIDDEN_FAILING_ROW_TEXT,
    POSTGRES_INSUFFICIENT_RESOURCES_SQLSTATE_CLASS,
    POSTGRES_MISSING_CONFLICT_TARGET_SQLSTATE,
    POSTGRES_MISSING_CONFLICT_TARGET_TEXT,
    POSTGRES_NULL_CHARACTER_SQLSTATES,
    POSTGRES_PASSWORD_ENVIRONMENT_VARIABLE,
    POSTGRES_POOL_CLOSED_WHILE_OPENING_MESSAGE,
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
    POSTGRES_UNUSABLE_CHANNEL_MESSAGE,
    POSTGRES_UNUSABLE_ROOT_CERTIFICATE_MESSAGE,
    POSTGRES_USER_SCHEMAS_SQL,
    POSTGRES_UTC_TIME_ZONE_NAMES,
    POSTGRES_VALUE_NULL_BYTE_MESSAGE,
    POSTGRESQL_CURRENT_SEARCH_PATH_SQL,
    POSTGRESQL_MISSING_TENANT_SCHEMA_MESSAGE,
    POSTGRESQL_POOLER_LOGIN_REFUSAL_SQLSTATE,
    POSTGRESQL_POOLER_LOGIN_REFUSAL_TEXT,
    POSTGRESQL_SCHEMA_EXISTS_SQL,
    POSTGRESQL_SET_TRANSACTION_SEARCH_PATH_SQL,
    POSTGRESQL_SINGLE_CONNECTION_POOL_SETTINGS,
    PYTHON_LONG_INT_ERROR_START,
    RETURNING_CLAUSE_RE,
    SQL_BLOCK_COMMENT_DELIMITER_RE,
    SQL_COMMENT_OR_QUOTED_TEXT_RE,
    SQL_QUOTED_TEXT_PLACEHOLDER,
    WRITE_STATEMENT_KEYWORDS,
    WRITE_STATEMENT_TYPE_CACHED_TEXT_MAX_LENGTH,
)
from hare.dialects.postgresql.client.reusable_test_schemas import ReusableTestSchemas
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
    OperationalError,
    ValidationError,
)
from hare.instrumentation.observers.observers import Observers
from hare.instrumentation.pools.pool_registry import PoolRegistry
from hare.sql.builder.queries.query import Query

if TYPE_CHECKING:
    from asyncpg.connection import Connection

    from hare.contrib.test.databases.kept_test_schema import KeptTestSchema

T = TypeVar("T")


CoroutineFunction = Callable[..., Coroutine[None, None, T]]


class PostgresqlClient(DatabaseClient, abc.ABC):
    """What the PostgreSQL drivers share: the pool and its health, the statement options, the errors told apart
    by SQLSTATE, and the test databases."""

    rejected_sql_character: ClassVar[str | None] = SQL_NULL_BYTE
    runs_statement_options: ClassVar[bool] = True

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
        if (
            isinstance(translated, DatabaseError)
            and getattr(error, "sqlstate", None) in POSTGRES_NULL_CHARACTER_SQLSTATES
        ):
            translated = ValidationError(POSTGRES_VALUE_NULL_BYTE_MESSAGE)
        elif isinstance(error, ValueError) and str(error).startswith(PYTHON_LONG_INT_ERROR_START):
            # The driver writes a bound int as text, which Python refuses past its digit limit.
            translated = ValidationError(SQL_LONG_INT_MESSAGE)
        if isinstance(translated, DatabaseError):
            if translated.sql is None:
                translated.sql = sql
                translated.parameters = parameters
            PostgresqlClient.hide_failing_row(translated)
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
    def get_statement_error(error: Exception) -> Exception:
        """The error of a statement the server refused - a bound text holding a null byte refused as
        every statement refuses it, any other refusal an ``OperationalError``.

        Args:
            error: The driver's error.

        Returns:
            The error to raise.
        """
        if getattr(error, "sqlstate", None) in POSTGRES_NULL_CHARACTER_SQLSTATES:
            return ValidationError(POSTGRES_VALUE_NULL_BYTE_MESSAGE)
        return OperationalError(error)

    @staticmethod
    def hide_failing_row(error: DatabaseError) -> None:
        """Writes ``<hidden>`` for the row a NOT NULL or CHECK violation names - a sensitive field's
        value among its columns; the driver's error, holding it too, isn't chained to the error.

        Args:
            error: The error.
        """
        # An argument is the driver's error itself on some drivers - its text is what is shown.
        texts = [argument if isinstance(argument, str) else str(argument) for argument in error.args]
        if not any(POSTGRES_FAILING_ROW_RE.search(text) for text in texts):
            return
        error.args = tuple(POSTGRES_FAILING_ROW_RE.sub(POSTGRES_HIDDEN_FAILING_ROW_TEXT, text) for text in texts)
        error.hides_driver_error = True

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

    @staticmethod
    def raise_if_root_certificate_unusable(path: str | None) -> None:
        """Rejects a root certificate file that can't be read or holds no certificate, before a
        connection is opened.

        Args:
            path: The file, None for none given.

        Raises:
            ConfigurationError: The file can't be read or holds no certificate.
        """
        if path is None:
            return
        try:
            ssl.create_default_context().load_verify_locations(cafile=path)
        except (OSError, ValueError) as error:
            # ssl.SSLError is an OSError too - a file holding no certificate; a ValueError a path with a null byte.
            reason = error.strerror if isinstance(error, OSError) and error.strerror else error
            raise ConfigurationError(
                POSTGRES_UNUSABLE_ROOT_CERTIFICATE_MESSAGE.format(path=path, reason=reason)
            ) from None

    @staticmethod
    def raise_if_channel_unusable(channel: str) -> None:
        """Rejects a LISTEN channel the server can't take, before a connection is opened for it.

        Args:
            channel: The channel name.

        Raises:
            ValidationError: The name is empty or holds a null byte.
        """
        if not channel or SQL_NULL_BYTE in channel:
            raise ValidationError(POSTGRES_UNUSABLE_CHANNEL_MESSAGE.format(channel=channel))

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
        self.reusable_test_database_lease: int | None = self.extra.pop(REUSABLE_TEST_DATABASE_LEASE_CREDENTIAL, None)
        #: The schema a leased test database kept from its previous lease - generate_schema() keeps it
        #: when the context asks for the same one, and resets the database otherwise.
        self.kept_test_schema: KeptTestSchema | None = None
        #: The schema a leased test database holds as its context created it - kept, rows cleared, when
        #: the lease ends with the schema unchanged.
        self.leased_test_schema: KeptTestSchema | None = None
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
        self.closes_started = 0
        self.opening_task = None
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
            ConfigurationError: The settings are no mapping (a DB_URL's ``?server_settings=`` text), or name a
                ``TimeZone`` other than UTC.
        """
        if server_settings is not None and not isinstance(server_settings, Mapping):
            raise ConfigurationError(
                f"server_settings must be a mapping of PostgreSQL settings to values, got {server_settings!r} - "
                "a DB_URL can't give one: set it in the connection's credentials"
            )
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
        """Creates the driver's pool of connections.

        Args:
            kwargs: The pool's settings.

        Returns:
            The pool.
        """
        raise NotImplementedError("create_pool is not implemented")

    async def _create_pool_with_retry(self, **kwargs: Any) -> Any:
        """Creates the pool, retrying on the driver's ``RETRYABLE_CONNECT_EXCEPTIONS`` with exponential
        backoff. Off by default (``connect_max_retries=0``).
        """
        attempt = 0
        renewed_password = False
        while True:
            try:
                return await self.create_pool(**kwargs)
            except Exception as error:
                if (
                    self.password_provider is not None
                    and not renewed_password
                    and self.is_authentication_failure(error)
                ):
                    # A rotated credential - the provider is asked for the new one, once.
                    renewed_password = True
                    await self.renew_password()
                    if not callable(kwargs.get("password")):
                        kwargs["password"] = await self.password_provider.get()
                    continue
                # A refused credential is the same on every attempt.
                will_retry = (
                    (isinstance(error, self.RETRYABLE_CONNECT_EXCEPTIONS) or self.is_server_out_of_resources(error))
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

    async def _pool_release(self, pool: Any, connection: Any) -> None:
        """Hands a connection back to the pool it was taken from.

        Args:
            pool: The pool.
            connection: The connection.
        """
        await pool.release(connection)

    async def _post_connect(self) -> None:
        await super()._post_connect()
        if self.transaction_pooling and "search_path" in self.server_settings:
            await TransactionPooler.raise_if_search_path_unreported(self)
            await TransactionPooler.check_search_path(self, self.server_settings["search_path"])
        if self.tenant_schema is not None:
            await self._raise_if_tenant_schema_missing()

    async def _raise_if_tenant_schema_missing(self) -> None:
        """Refuses the client of a tenant's schema the database doesn't hold - its pool closed, so the
        next use, once the schema is created, checks again.

        Raises:
            ConfigurationError: The schema doesn't exist.
        """
        if await self.execute_dicts(POSTGRESQL_SCHEMA_EXISTS_SQL, [self.tenant_schema]):
            return
        await self.close_refused_pool()
        raise ConfigurationError(POSTGRESQL_MISSING_TENANT_SCHEMA_MESSAGE.format(schema=self.tenant_schema))

    async def close_refused_pool(self) -> None:
        """Closes the pool just opened for a connection its setup refused - not the client: the next
        use opens the pool and checks it again."""
        pool, self._pool = self._pool, None
        await self._close_pool(pool)

    def get_server_client(self) -> PostgresqlClient:
        """The client of the server past the transaction pooler (``direct_host``) - this client for a
        connection going straight to the server.

        Raises:
            ConfigurationError: The connection goes through a pooler and names no direct way to the
                server.
        """
        return TransactionPooler.get_direct_client(self)

    def get_pool_bounds(self) -> tuple[int, int]:
        return self.pool_minsize, self.pool_maxsize

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
    async def _close(self) -> None:
        raise NotImplementedError("_close is not implemented")

    @abc.abstractmethod
    async def _close_pool(self, pool: Any) -> None:
        """Closes a pool the client no longer holds.

        Args:
            pool: The driver's pool.
        """
        raise NotImplementedError("_close_pool is not implemented")

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
        self.closes_started += 1
        # Taken before anything is awaited: a pool opening after the close started (a query after
        # Connections.reconnect()) has a template of its own, which it reads once open.
        template = self._template
        PoolRegistry.remove(self)
        direct_client = self._direct_client
        if direct_client is not None:
            self._direct_client = None
            await direct_client.close()
        await self.close_tenant_clients()
        await self._close()
        # Unless a pool opened meanwhile (a query after Connections.reconnect()) took its own.
        if self._template is template:
            self._template.clear()

    async def enter_tenant_schema(self, schema_name: str) -> str:
        """Runs the rest of the transaction in another tenant's schema: its search path starts with
        that schema in place of the transaction's own tenant schema.

        Args:
            schema_name: The tenant's schema.

        Returns:
            The search path it replaced.
        """
        (row,) = await self.execute_dicts(POSTGRESQL_CURRENT_SEARCH_PATH_SQL)
        search_path = row["search_path"]
        schemas = TransactionPooler.get_search_path_schemas(search_path)
        if self.tenant_schema is not None and schemas[:1] == (self.tenant_schema,):
            schemas = schemas[1:]
        quote_identifier = self.dialect.literals.quote_identifier
        await self.execute_dicts(
            POSTGRESQL_SET_TRANSACTION_SEARCH_PATH_SQL,
            [",".join(quote_identifier(schema) for schema in (schema_name, *schemas))],
        )
        return search_path

    async def leave_tenant_schema(self, previous_schemas: str) -> None:
        """Brings the transaction back to the search path ``enter_tenant_schema()`` replaced.

        Args:
            previous_schemas: The search path.
        """
        await self.execute_dicts(POSTGRESQL_SET_TRANSACTION_SEARCH_PATH_SQL, [previous_schemas])

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
    def is_missing_conflict_target_error(error: BaseException) -> bool:
        """Whether an INSERT failed because its ``ON CONFLICT`` names columns no unique index or
        constraint of the table has (SQLSTATE 42P10).

        Args:
            error: The exception, hare's or the driver's.

        Returns:
            True for that refusal.
        """
        sqlstate = getattr(error, "sqlstate", None) or getattr(error.__cause__, "sqlstate", None)
        return sqlstate == POSTGRES_MISSING_CONFLICT_TARGET_SQLSTATE and POSTGRES_MISSING_CONFLICT_TARGET_TEXT in str(
            error
        )

    @staticmethod
    def is_server_out_of_resources(error: BaseException) -> bool:
        """Whether a refused connection is the server out of a resource - SQLSTATE class 53: too
        many clients, no memory or disk left.

        Args:
            error: The driver's error.

        Returns:
            True for such a refusal - it passes once the server has the resource again.
        """
        return str(getattr(error, "sqlstate", None) or "").startswith(POSTGRES_INSUFFICIENT_RESOURCES_SQLSTATE_CLASS)

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
        already reset it, takes the schema it kept for a context creating its models' schema
        (``generate_schema()`` decides on it), resets it when it exists from before, creates it
        otherwise."""
        from hare.contrib.test.databases.reusable_test_databases import ReusableTestDatabases

        assert self.database is not None and self.reusable_test_database_lease is not None  # nosec B101
        self.leased_test_schema = None
        self.kept_test_schema = ReusableTestDatabases.take_kept_schema(
            self.database, self.reusable_test_database_lease
        )
        if self.kept_test_schema is None and not ReusableTestDatabases.is_clean(self.database):
            if await self._database_exists():
                await self._reset_database()
            else:
                await self._create_database()
        ReusableTestDatabases.mark_used(self.database)

    async def _release_reusable_test_database(self) -> None:
        """Returns a leased test database to the pool: keeps the schema its context created, the rows
        cleared, when the schema is unchanged, and resets the database otherwise - also when that
        fails, leaving it marked as needing a reset before its next use."""
        from hare.contrib.test.databases.reusable_test_databases import ReusableTestDatabases

        assert self.database is not None and self.reusable_test_database_lease is not None  # nosec B101
        if not ReusableTestDatabases.is_leased(self.database, self.reusable_test_database_lease):
            return  # already released - the slot may belong to another context by now
        leased_test_schema, self.leased_test_schema = self.leased_test_schema, None
        self.kept_test_schema = None
        is_clean = False
        kept_schema = None
        try:
            if leased_test_schema is not None:
                kept_schema = await self._run_on_server_session(
                    lambda: self._clear_rows_on_its_session(leased_test_schema)
                )
            if kept_schema is None:
                is_clean = await self._reset_database()
        finally:
            ReusableTestDatabases.release(
                self.database, self.reusable_test_database_lease, is_clean=is_clean, kept_schema=kept_schema
            )

    async def generate_schema(self, safe: bool) -> None:
        """Creates every model of this connection in the database - on a leased test database holding
        the same schema from its previous lease, nothing: the schema stands, its rows cleared.

        Args:
            safe: Whether each object is created only when it doesn't exist yet.
        """
        from hare.contrib.test.databases.reusable_test_databases import ReusableTestDatabases

        if self.reusable_test_database_lease is None:
            await super().generate_schema(safe)
            return
        # Connected first, so the DDL is written for the features of the server's own version.
        async with self.acquire_connection():
            pass
        schema = self.get_schema_sql(safe)
        schema_key = ReusableTestSchemas.get_schema_key(schema)
        kept_test_schema, self.kept_test_schema = self.kept_test_schema, None
        if kept_test_schema is not None:
            if kept_test_schema.schema_key == schema_key:
                self.leased_test_schema = kept_test_schema
                return
            await self._reset_database()
        self.log.debug("Creating schema: %s", schema)
        if schema:
            await self.execute_script(schema)
        # Only the schema a context creates first thing is kept - a later call may follow DDL of the test.
        if self.leased_test_schema is None and ReusableTestDatabases.may_keep_schema(
            self.reusable_test_database_lease
        ):
            self.leased_test_schema = await ReusableTestSchemas.get_kept_schema(self, schema_key)

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
        return await self._run_on_server_session(self._reset_database_on_its_session)

    async def _run_on_server_session(self, operation: Callable[[], Coroutine[Any, Any, T]]) -> T:
        """Runs a reset of the database on a session of the server's own past a transaction pooler: a
        pooled session it ran on would stay in the pooler, the dropped extensions' objects cached -
        PostGIS keeps their ids per session, and a query of a later test failed on the extension
        created again.

        Args:
            operation: The reset, connecting and closing the client itself.

        Returns:
            What the operation returns.
        """
        pooled_address = self.host, self.port
        if self.transaction_pooling and self.direct_host is not None:
            self.host, self.port = self.direct_host, self.direct_port or self.port
        try:
            return await operation()
        finally:
            self.host, self.port = pooled_address

    async def _end_other_sessions(self) -> None:
        """Ends the database's other sessions and rolls back its prepared transactions."""
        await self.execute(POSTGRES_TERMINATE_OTHER_DATABASE_SESSIONS_SQL)
        for prepared_transaction_row in await self.execute_dicts(POSTGRES_DATABASE_PREPARED_TRANSACTIONS_SQL):
            gid_literal = self.dialect.literals.get_string_literal_sql(prepared_transaction_row["gid"])
            await self.execute_script(POSTGRES_ROLLBACK_PREPARED_SQL.format(gid_literal=gid_literal))

    async def _get_settings_reset_statements(self) -> list[str]:
        """The statements clearing the database's settings and its roles' settings in it.

        Returns:
            The statements.
        """
        role_setting_rows = await self.execute_dicts(POSTGRES_DATABASE_ROLE_SETTINGS_SQL)
        quoted_database = self.dialect.literals.quote_identifier(self.database or "")
        statements = [POSTGRES_RESET_DATABASE_SETTINGS_SQL.format(database=quoted_database)]
        statements.extend(
            POSTGRES_RESET_DATABASE_ROLE_SETTINGS_SQL.format(
                role=role_setting_row["role_name"], database=quoted_database
            )
            for role_setting_row in role_setting_rows
        )
        return statements

    async def _clear_rows_on_its_session(self, leased_test_schema: KeptTestSchema) -> KeptTestSchema | None:
        """Empties a leased test database whose schema is unchanged, keeping the schema - on the
        session the client's address opens.

        Args:
            leased_test_schema: The schema the lease's context created.

        Returns:
            The schema kept, or None when the database has to be reset instead.
        """
        try:
            await self._reconnect(with_db=True)
        except ConfigurationError:
            await self.close()
            return None
        try:
            await self._end_other_sessions()
            kept_schema = await ReusableTestSchemas.clear_rows(self, leased_test_schema)
            if kept_schema is not None:
                await self.execute_script(";\n".join(await self._get_settings_reset_statements()))
            return kept_schema
        finally:
            await self.close()

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
            await self._end_other_sessions()
            schema_rows = await self.execute_dicts(POSTGRES_USER_SCHEMAS_SQL)
            version_rows = await self.execute_dicts(POSTGRES_SERVER_VERSION_NUMBER_SQL)
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
            statements.extend(await self._get_settings_reset_statements())
            await self.execute_script(";\n".join(statements))
        finally:
            await self.close()
        return True

    def acquire_connection(self) -> ConnectionWrapper[Any] | PoolConnectionWrapper[Any]:
        return PoolConnectionWrapper(self)

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
        """Creates the pool on first use - once, whichever task gets there first.

        Raises:
            DBConnectionError: The connection was closed and dropped, or - for a setup statement of the
                pool being opened - closed meanwhile.
        """
        opening_task = self.opening_task
        if self._pool and opening_task is not None and opening_task is not asyncio.current_task():
            # Another task's pool is open but still being set up - the server's version and features,
            # the checks of the connection: a statement runs once that is done.
            async with self._pool_init_lock:
                pass
        if not self._pool:
            if self.opening_task is not None and self.opening_task is asyncio.current_task():
                # A setup statement of the pool being opened, closed meanwhile - the opening task holds
                # the lock opening it would wait on, and opens it anew.
                raise DBConnectionError(POSTGRES_POOL_CLOSED_WHILE_OPENING_MESSAGE)
            async with self._pool_init_lock:
                if not self._pool:
                    # A pool that opened across a close (Connections.reconnect()) was closed again -
                    # the query runs on a new one.
                    while not self._pool and not self.discarded:
                        await self.open_across_closes()
                    if self.discarded:
                        # Dropped while its pool opened - closed again, never left open.
                        await self._close()
                        raise DBConnectionError(
                            f"The connection {self.connection_alias!r} was closed and dropped - a query runs on "
                            "the connection the context gives now"
                        )

    async def acquire_from_open_pool(self) -> tuple[Any, Any]:
        """Checks out one connection of the pool, opening it first - of the new pool when the one it
        waited on was closed meanwhile (``Connections.reconnect()``): a query that had no connection
        yet runs on the new pool.

        Returns:
            The pool and the connection.
        """
        while True:
            if self.discarded and (successor := self.get_successor()) is not None:
                # Discarded by a repeated Hare.init() - the client replacing it gives the connection.
                return await successor.acquire_from_open_pool()
            try:
                await self._ensure_connection()
            except DBConnectionError:
                if self.get_successor() is None:
                    raise
                continue
            pool = self._pool
            if pool is None:
                continue
            try:
                connection = await self._pool_acquire()
            except Exception:
                if self.get_successor() is not None:
                    continue
                if self._pool is pool or self.discarded:
                    raise
                continue
            if self._pool is not pool:
                # The pool was closed while this waited for it - a connection it hands out once its close
                # began is closed under the query; given back, and the new pool gives one.
                await self._pool_release(pool, connection)
                continue
            return pool, connection

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
