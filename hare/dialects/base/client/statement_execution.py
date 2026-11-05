from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from functools import partial, wraps
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.base.client.constants import READ_RETRY_MESSAGE, UNENCODABLE_SURROGATE_MESSAGE
from hare.dialects.base.client.declarations import StatementRequest
from hare.dialects.base.client.enums import StatementForm
from hare.exceptions import DatabaseError, DBConnectionError, TransactionManagementError, ValidationError
from hare.instrumentation.declarations import QueryCall
from hare.instrumentation.observers.observers import Observers
from hare.instrumentation.queries.query_tags import QueryTags
from hare.sql.terms.parameters.query_parameters import QueryParameters

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.client.transaction_client import TransactionClient


class StatementExecution:
    """The shared statement boundary around a driver's wire operation.

    Binding and tagging happen once, before query wrappers. Each invocation carries its
    retry permission explicitly; transaction control and bulk loading never retry reads.
    Drivers retain acquisition and wire cleanup, so returning a result or raising happens
    only after their lease contract is satisfied. Unknown write outcomes are never retried.
    """

    @staticmethod
    def decorate(
        client_class: type[DatabaseClient], form: StatementForm, function: Callable[..., Any]
    ) -> Callable[..., Any]:
        """Wrap a declared call shape; the function name is only an observer label."""
        observed = form is not StatementForm.CONTROL

        @wraps(function)
        async def execute(client: DatabaseClient, *args: Any, **kwargs: Any) -> Any:
            args, kwargs, request = StatementExecution.bind(client, form, args, kwargs)
            operation = partial(StatementExecution.run, client_class, form, function, client, request, args, kwargs)
            if observed and Observers.query_wrappers:
                call = QueryCall(
                    function.__name__,
                    cast("str", request.sql),
                    request.parameters,
                    client.connection_alias,
                    client.dialect,
                )
                return await Observers.run_wrapped(call, operation)
            return await operation()

        return execute

    @staticmethod
    def bind(
        client: DatabaseClient, form: StatementForm, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> tuple[tuple[Any, ...], dict[str, Any], StatementRequest]:
        """Bind the explicit SQL/parameters or bulk-load metadata without parsing SQL."""
        retry_read = kwargs.pop("retry_read", False)
        recovery = kwargs.pop("recovery", None)
        if form is StatementForm.CONTROL:
            return args, kwargs, StatementRequest(None)
        if form is StatementForm.BULK_LOAD:
            table = args[0] if args else kwargs["table"]
            columns = args[1] if len(args) > 1 else kwargs["columns"]
            bulk_sql = client.dialect.clauses.get_bulk_load_statement_sql(table, columns)
            return args, kwargs, StatementRequest(QueryTags.append(bulk_sql, client.dialect))
        sql = args[0] if args else kwargs.get("query")
        parameters = args[1] if len(args) > 1 else kwargs.get("values")
        if sql is not None and QueryTags.current.get():
            tagged_sql = QueryTags.append(sql, client.dialect)
            if tagged_sql is not sql:
                if args:
                    args = (tagged_sql, *args[1:])
                else:
                    kwargs = {**kwargs, "query": tagged_sql}
                sql = tagged_sql
        return args, kwargs, StatementRequest(sql, parameters, retry_read and form is StatementForm.SQL, recovery)

    @staticmethod
    async def run(
        client_class: type[DatabaseClient],
        form: StatementForm,
        function: Callable[..., Any],
        client: DatabaseClient,
        request: StatementRequest,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        """Retry only explicitly safe reads outside a transaction, after translated failures."""
        retry_limit = (
            client.read_retry_max_retries
            if request.retry_read and client_class.runs_statement_options and not client.is_transaction_client
            else 0
        )
        attempt = 0
        while True:
            try:
                return await StatementExecution.attempt(client_class, form, function, client, request, args, kwargs)
            except DBConnectionError as error:
                if attempt >= retry_limit:
                    raise
                delay = client.read_retry_backoff_base_seconds * (2**attempt)
                client.log.debug(READ_RETRY_MESSAGE, attempt + 1, retry_limit, error, delay)
                await asyncio.sleep(delay)
                attempt += 1

    @staticmethod
    async def attempt(
        client_class: type[DatabaseClient],
        form: StatementForm,
        function: Callable[..., Any],
        client: DatabaseClient,
        request: StatementRequest,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        """Execute with transaction guards, timeout, error translation and one event per attempt."""
        observed = form is not StatementForm.CONTROL
        has_sql = form in {StatementForm.SQL, StatementForm.MANY, StatementForm.SCRIPT}
        sql, parameters = request.sql, request.parameters
        rejected_character = client_class.rejected_sql_character
        if rejected_character is not None and sql is not None and rejected_character in sql:
            client.raise_rejected_sql_character(sql)
        start = time.perf_counter()
        exception: BaseException | None = None
        try:
            try:
                if form is not StatementForm.CONTROL and client.is_transaction_client:
                    transaction = cast("TransactionClient", client)
                    transaction._check_statement_allowed()
                    if client_class.checks_aborted_transactions and transaction._is_transaction_aborted():
                        raise TransactionManagementError(client.aborted_transaction_message)
                if not client_class.runs_statement_options:
                    return await function(client, *args, **kwargs)

                async def timed(client: Any, *args: Any, **kwargs: Any) -> Any:
                    return await client._run_with_command_timeout(function, *args, **kwargs)

                operation = timed if observed and client.command_timeout is not None else function
                if client.password_provider is not None and not client.is_transaction_client:
                    operation = partial(
                        cast("Callable[..., Any]", client_class.run_renewing_refused_password), operation
                    )
                return await operation(client, *args, **kwargs)
            except BaseException as driver_error:
                original_error = driver_error
                exception, hides_context = StatementExecution.get_error(
                    client_class, client, request, driver_error, args, has_sql
                )
            # Recovery runs outside the driver's exception handler: cancellation of
            # cleanup must not inherit a driver error containing sensitive values.
            if request.recovery is not None:
                await request.recovery.recover(client, original_error)
            if not hides_context:
                if exception is original_error:
                    raise exception
                raise exception from original_error
        finally:
            if observed:
                Observers.record_query(sql, parameters, start, exception, client.connection_alias)
        # Sensitive driver errors are raised outside their except block: no context leak.
        raise exception

    @staticmethod
    def get_error(
        client_class: type[DatabaseClient],
        client: DatabaseClient,
        request: StatementRequest,
        driver_error: BaseException,
        args: tuple[Any, ...],
        has_sql: bool,
    ) -> tuple[BaseException, bool]:
        """Translate an error and say whether its driver context must be discarded."""
        if client_class.is_unencodable_surrogate_error(driver_error):
            return ValidationError(UNENCODABLE_SURROGATE_MESSAGE), True
        error = client_class.translate_driver_error(
            client, driver_error, request.sql, request.parameters, args, has_sql
        )
        hidden_values = QueryParameters.get_hidden_values_of(request.parameters)
        if isinstance(error, DatabaseError) and (hidden_values or error.hides_driver_error):
            error.drop_values(hidden_values)
            return error, True
        return error, False

    @staticmethod
    async def stream(
        client: DatabaseClient, query: str, values: list[Any] | None, chunk_size: int
    ) -> AsyncGenerator[list[Any]]:
        """Observe one stream through wire cleanup; a partially read stream never retries."""
        query = QueryTags.append(query, client.dialect)
        request = StatementRequest(query, values)
        start = time.perf_counter()
        exception: BaseException | None = None
        try:
            if client.is_transaction_client:
                cast("TransactionClient", client)._check_statement_allowed()
            if client.rejected_sql_character is not None and client.rejected_sql_character in query:
                client.raise_rejected_sql_character(query)
            if Observers.query_wrappers:
                call = QueryCall("stream", query, values, client.connection_alias, client.dialect)
                batches = Observers.stream_wrapped(
                    call, lambda: client._driver_stream_batches(query, values, chunk_size)
                )
            else:
                batches = client._driver_stream_batches(query, values, chunk_size)
            try:
                async for batch in batches:
                    yield batch
            finally:
                if (close_batches := getattr(batches, "aclose", None)) is not None:
                    # Query wrappers may restore ContextVar tokens when they close;
                    # their generator must finish in the task that entered it.
                    await close_batches()
        except GeneratorExit:
            raise
        except BaseException as driver_error:
            exception, hides_context = StatementExecution.get_error(
                type(client), client, request, driver_error, (query, values), True
            )
            if not hides_context:
                if exception is driver_error:
                    raise
                raise exception from driver_error
        finally:
            Observers.record_query(query, values, start, exception, client.connection_alias)
        if exception is not None:
            raise exception
