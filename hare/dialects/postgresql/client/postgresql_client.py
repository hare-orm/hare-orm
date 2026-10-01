from __future__ import annotations

import abc
import asyncio
import time
import uuid
from asyncio.events import AbstractEventLoop
from collections.abc import Callable, Coroutine
from functools import partial, wraps
from typing import TYPE_CHECKING, Any, SupportsInt, TypeVar, cast

from hare.dialects.base.client.connection_wrapper import ConnectionWrapper
from hare.dialects.base.client.database_client import DatabaseClient, retryable_read_query_active
from hare.dialects.base.client.pool_connection_wrapper import PoolConnectionWrapper
from hare.dialects.base.constants import (
    COMMAND_TIMEOUT_METHOD_NAMES,
    OBSERVED_METHOD_NAMES,
    QUERY_EXECUTING_METHOD_NAMES,
)
from hare.dialects.base.features import Features
from hare.dialects.base.results.statement_result import StatementResult
from hare.dialects.postgresql.constants import (
    CTE_DEFINITION_RE,
    CTE_INTRODUCER_RE,
    LEADING_WORD_RE,
    PARENTHESIZED_GROUP_RE,
    POSTGRES_CONNECTION_OPTION_DEFAULTS,
    POSTGRES_CONNECTION_OPTIONS,
    POSTGRES_CREATE_LEGACY_PUBLIC_SCHEMA_STATEMENTS,
    POSTGRES_CREATE_PUBLIC_SCHEMA_STATEMENTS,
    POSTGRES_DATABASE_EXISTS_SQL,
    POSTGRES_DATABASE_OWNER_PUBLIC_SCHEMA_VERSION_NUMBER,
    POSTGRES_DATABASE_PREPARED_TRANSACTIONS_SQL,
    POSTGRES_DATABASE_ROLE_SETTINGS_SQL,
    POSTGRES_DEFAULT_PORT,
    POSTGRES_DROP_SCHEMA_CASCADE_SQL,
    POSTGRES_PORT_OPTION,
    POSTGRES_RESET_DATABASE_ROLE_SETTINGS_SQL,
    POSTGRES_RESET_DATABASE_SETTINGS_SQL,
    POSTGRES_ROLLBACK_PREPARED_SQL,
    POSTGRES_SERVER_VERSION_NUMBER_MAJOR_FACTOR,
    POSTGRES_SERVER_VERSION_NUMBER_SQL,
    POSTGRES_SESSION_TIME_ZONE,
    POSTGRES_SESSION_TIME_ZONE_SETTING,
    POSTGRES_SET_LOCAL_LOCK_TIMEOUT_SQL,
    POSTGRES_SET_LOCAL_STATEMENT_TIMEOUT_SQL,
    POSTGRES_SET_TRANSACTION_READ_ONLY_SQL,
    POSTGRES_STATEMENT_NULL_BYTE_MESSAGE,
    POSTGRES_TERMINATE_OTHER_DATABASE_SESSIONS_SQL,
    POSTGRES_USER_SCHEMAS_SQL,
    POSTGRES_UTC_TIME_ZONE_NAMES,
    POSTGRESQL_DIALECT,
    RETURNING_CLAUSE_RE,
    REUSABLE_TEST_DATABASE_LEASE_CREDENTIAL,
    SQL_BLOCK_COMMENT_DELIMITER_RE,
    SQL_COMMENT_OR_QUOTED_TEXT_RE,
    SQL_QUOTED_TEXT_PLACEHOLDER,
    WRITE_STATEMENT_KEYWORDS,
)
from hare.dialects.postgresql.query.postgresql_query import PostgresqlQuery
from hare.exceptions import (
    ConfigurationError,
    DatabaseError,
    DBConnectionError,
    ValidationError,
)
from hare.instrumentation.observers import Observers
from hare.instrumentation.query_call import QueryCall
from hare.instrumentation.query_tags import QueryTags
from hare.sql.constants import SQL_NULL_BYTE
from hare.transactions.options import TransactionOptions

if TYPE_CHECKING:
    from asyncpg.connection import Connection

T = TypeVar("T")


FuncType = Callable[..., Coroutine[None, None, T]]


class PostgresqlClient(DatabaseClient, abc.ABC):
    @staticmethod
    def translate_exceptions(func: FuncType[Any]) -> FuncType[Any]:
        """Wraps a client method: appends the query tags to its SQL, runs it inside the query
        wrappers, raises the driver's exceptions as hare's (``get_driver_error()``) and reports the
        query to the observers.

        Args:
            func: The method.

        Returns:
            The wrapped method.
        """

        def get_translating_method(wrapped_method: FuncType[Any] | None) -> FuncType[Any]:
            # wrapped_method: the method the query wrappers run, its SQL tagged already - None
            # for that method itself.
            method_name = func.__name__
            is_query_executing = method_name in QUERY_EXECUTING_METHOD_NAMES
            is_observed = method_name in OBSERVED_METHOD_NAMES
            is_command_timeout_bounded = method_name in COMMAND_TIMEOUT_METHOD_NAMES
            tags_sql = wrapped_method is not None
            runs_query_wrappers = is_observed and wrapped_method is not None

            async def bound_by_command_timeout(client: Any, *args: Any, **kwargs: Any) -> Any:
                return await client._run_with_command_timeout(func, *args, **kwargs)

            async def translating_method(self: Any, *args: Any, **kwargs: Any) -> Any:
                if is_query_executing and not (tags_sql and QueryTags.current.get()):
                    sql = args[0] if args else kwargs.get("query")
                    params = args[1] if len(args) > 1 else kwargs.get("values")
                else:
                    args, kwargs, sql, params = DatabaseClient.get_tagged_query_arguments(args, kwargs, method_name)
                if runs_query_wrappers and Observers.query_wrappers:
                    call = QueryCall(method_name, cast("str", sql), params, self.connection_name, self.dialect)
                    return await Observers.run_wrapped(
                        call, partial(cast("FuncType[Any]", wrapped_method), self, *args, **kwargs)
                    )
                if sql is not None and SQL_NULL_BYTE in sql:
                    # Rejected before it is sent, so an open transaction isn't aborted by it.
                    PostgresqlClient.raise_if_statement_has_null_byte(sql)
                start = time.monotonic()
                exception: Exception | None = None
                # False for the retry branch, which records each of its attempts itself.
                record_here = True
                is_transaction_client = self.is_transaction_client
                try:
                    # A stale reference to a finished transaction or savepoint must not run a query -
                    # its connection may already serve another transaction.
                    if is_query_executing and is_transaction_client:
                        self._check_statement_allowed()
                    if is_command_timeout_bounded and self.command_timeout is not None:
                        method = bound_by_command_timeout
                    else:
                        method = func
                    # Only a client outside a transaction retries a read: a transaction's pinned
                    # connection is dead, and the read would run outside its isolation.
                    if (
                        is_query_executing
                        and self.read_retry_max_retries
                        and not is_transaction_client
                        and retryable_read_query_active.get()
                    ):
                        record_here = False
                        return await self._execute_read_query_with_retry(
                            method, *args, sql=sql, params=params, **kwargs
                        )
                    return await method(self, *args, **kwargs)
                except BaseException as exc:
                    error: BaseException = exc
                    if isinstance(exc, Exception):
                        error = exception = self.get_driver_error(exc, args) or exc
                    if isinstance(error, DatabaseError) and error.sql is None:
                        error.sql = sql
                        error.params = params
                    if is_transaction_client:
                        if is_query_executing and isinstance(error, asyncio.CancelledError):
                            self._mark_statement_interrupted()
                        else:
                            self._mark_statement_failed()
                    if error is exc:
                        raise
                    raise error from exc
                finally:
                    if is_observed and record_here:
                        Observers.record_query(sql, params, start, exception, self.connection_name)

            return translating_method

        return wraps(func)(get_translating_method(get_translating_method(None)))

    query_class: type[PostgresqlQuery] = PostgresqlQuery
    native_python_types = DatabaseClient.native_python_types | {bool, uuid.UUID}

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
    features = Features(
        supports_nulls_distinct=True,
        supports_partitioned_exclusion_constraints=True,
        supports_update_limit_order_by=False,
        supports_posix_regex=True,
        supports_select_for_no_key_update=True,
        can_rollback_ddl=True,
        supports_returning=True,
        supports_positional_rows=True,
        supports_streaming=True,
        supports_two_phase_commit=True,
        supports_listen_notify=True,
    )
    connection_class: Connection | None = None
    loop: AbstractEventLoop | None = None
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
        self.extra.pop("connection_name", None)
        self.extra.pop("fetch_inserted", None)
        self.reusable_test_database_lease: int | None = self.extra.pop(REUSABLE_TEST_DATABASE_LEASE_CREDENTIAL, None)
        self.loop = self.extra.pop("loop", None)
        self.connection_class = self.extra.pop("connection_class", self.connection_class)
        settings = POSTGRES_CONNECTION_OPTIONS.read(self.extra, POSTGRES_CONNECTION_OPTION_DEFAULTS)
        self.schema = settings["schema"]
        self.application_name = settings["application_name"]
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

    def _get_connection_failure_message(self, with_db: bool, exception: BaseException) -> str:
        """The message for a failed connection attempt, carrying the driver's own reason.

        Args:
            with_db: Whether the configured database (rather than the server default) was used.
            exception: The driver exception.

        Returns:
            The message.
        """
        if with_db:
            return f"Can't establish connection to database {self.database}. Exception: {exception}"
        return f"Can't establish connection to default database. Verify environment PGDATABASE. Exception: {exception}"

    async def _run_with_command_timeout(self, func: FuncType[Any], *args: Any, **kwargs: Any) -> Any:
        """Runs ``func(self, ...)``, cancelling it once ``command_timeout`` elapses. Enforced here:
        asyncpg's own option waits for its cancel request to be answered, and rust_pg has none.

        Raises:
            TimeoutError: The call outlived ``command_timeout``.
        """
        try:
            async with asyncio.timeout(self.command_timeout) as timeout_scope:
                return await func(self, *args, **kwargs)
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
            except self.RETRYABLE_CONNECT_EXCEPTIONS as exc:
                if attempt >= self.connect_max_retries:
                    raise
                delay = self.connect_retry_backoff_base_seconds * (2**attempt)
                self.log.debug(
                    "Connection attempt %d/%d failed (%s), retrying in %.2fs",
                    attempt + 1,
                    self.connect_max_retries,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                attempt += 1

    async def _execute_read_query_with_retry(
        self,
        func: FuncType[Any],
        *args: Any,
        sql: str | None = None,
        params: list[Any] | None = None,
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
            attempt_start = time.monotonic()
            try:
                result = await self._translate_exceptions(func, *args, **kwargs)
            except DBConnectionError as exc:
                Observers.record_query(sql, params, attempt_start, exc, self.connection_name)
                if attempt >= self.read_retry_max_retries:
                    raise
                delay = self.read_retry_backoff_base_seconds * (2**attempt)
                self.log.debug(
                    "Read query attempt %d/%d failed (%s), retrying in %.2fs",
                    attempt + 1,
                    self.read_retry_max_retries,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                attempt += 1
            except Exception as exc:
                Observers.record_query(sql, params, attempt_start, exc, self.connection_name)
                raise
            else:
                Observers.record_query(sql, params, attempt_start, None, self.connection_name)
                return result

    async def _pool_acquire(self) -> Any:
        """Takes one raw connection from the pool - each driver overrides it with its own timeout and
        exception translation.
        """
        return await self._pool.acquire()

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

    async def _translate_exceptions(self, func: FuncType[Any], *args: Any, **kwargs: Any) -> Any:
        """Runs ``func(self, ...)``, raising the driver's exceptions as hare's.

        Args:
            func: The method.
            args: Its positional arguments.
            kwargs: Its keyword arguments.

        Returns:
            What it returned.
        """
        try:
            return await func(self, *args, **kwargs)
        except Exception as exc:
            error = self.get_driver_error(exc, args)
            if error is None:
                raise
            raise error from exc

    async def close(self) -> None:
        await self._close()
        self._template.clear()

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
        statement = f"CREATE DATABASE {self.dialect.quote_identifier(self.database)}"
        # No configured user means the driver connects as the default (PGUSER/OS) role, which then
        # owns the new database - there is no role name to name here.
        if self.user:
            statement += f" OWNER {self.dialect.quote_identifier(self.user)}"
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
        try:
            await self.execute_script(f"DROP DATABASE {self.dialect.quote_identifier(self.database)}")
        finally:
            await self.close()

    async def _prepare_reusable_test_database(self) -> None:
        """Makes a leased test database exist and be empty: reuses it as is when this process
        already reset it, resets it when it exists from before, creates it otherwise."""
        from hare.contrib.test.reusable_databases import ReusableTestDatabases

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
        from hare.contrib.test.reusable_databases import ReusableTestDatabases

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
                gid_literal = self.dialect.get_string_literal_sql(prepared_transaction_row["gid"])
                await self.execute_script(POSTGRES_ROLLBACK_PREPARED_SQL.format(gid_literal=gid_literal))
            schema_rows = await self.execute_dicts(POSTGRES_USER_SCHEMAS_SQL)
            role_setting_rows = await self.execute_dicts(POSTGRES_DATABASE_ROLE_SETTINGS_SQL)
            version_rows = await self.execute_dicts(POSTGRES_SERVER_VERSION_NUMBER_SQL)
            quoted_database = self.dialect.quote_identifier(self.database or "")
            statements = [
                POSTGRES_DROP_SCHEMA_CASCADE_SQL.format(schema=self.dialect.quote_identifier(schema_row["nspname"]))
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

    def _get_transaction_restriction_statements(self, options: TransactionOptions) -> list[str]:
        """``SET TRANSACTION ISOLATION LEVEL`` first - it must precede every query of the
        transaction - then ``SET TRANSACTION READ ONLY`` plus transaction-local statement and lock
        timeouts, the lock timeout equal to the statement timeout so a statement waiting on a lock
        is cancelled as well.

        Args:
            options: The transaction's options.

        Returns:
            The statements to run right after BEGIN, in order.
        """
        statements = self._get_isolation_statements(options)
        if options.read_only:
            statements.append(POSTGRES_SET_TRANSACTION_READ_ONLY_SQL)
        if (statement_timeout := options.statement_timeout) is not None:
            milliseconds = max(1, round(statement_timeout * 1000))
            statements.append(POSTGRES_SET_LOCAL_STATEMENT_TIMEOUT_SQL.format(milliseconds=milliseconds))
            statements.append(POSTGRES_SET_LOCAL_LOCK_TIMEOUT_SQL.format(milliseconds=milliseconds))
        return statements

    @abc.abstractmethod
    async def execute_many(self, query: str, values: list[Any]) -> None:
        raise NotImplementedError("execute_many is not implemented")

    @abc.abstractmethod
    async def execute(
        self, query: str, values: list[Any] | None = None, *, returns_rows: bool | None = None
    ) -> StatementResult:
        raise NotImplementedError("execute is not implemented")

    @translate_exceptions
    async def execute_script(self, query: str) -> None:
        async with self.acquire_connection() as connection:
            self.log.debug(query)
            await connection.execute(query)

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
