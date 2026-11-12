from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from hare.core.registries import Registries
from hare.dialects.base.client.transaction_client import TransactionClient
from hare.exceptions import QueryError
from hare.fields.encrypted.field_encryption import FieldEncryption
from hare.query.execution.messages import DEPENDENCY_REQUIRES_EXECUTION, SELECT_FOR_UPDATE_OUTSIDE_TRANSACTION
from hare.query.expressions import Q
from hare.query.scopes.row_scope_binding import RowScopeBinding

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.query.execution.query_dependency import QueryDependency
    from hare.query.queryset.query_specification import QuerySpecification


TResult = TypeVar("TResult")


class QueryExecution:
    """Transient dependency values for one execution, including its nested statements.

    Values are loaded once per execution and reused by nested statements and iterator pages.
    The task context is installed only while executing code: yielding an iterator item never
    leaves this context in its consumer. Concurrent and repeated executions remain independent.
    """

    current: ClassVar[ContextVar[dict[QueryDependency, Q | None] | None]] = ContextVar(
        "hare_query_dependencies", default=None
    )
    #: The conditions of an execution started outside any other - shared, never changed in place.
    EMPTY_CONDITIONS: ClassVar[dict[QueryDependency, Q | None]] = {}

    __slots__ = ("conditions", "snapshot", "encryption", "row_scopes", "tokens", "conditions_are_shared")

    def __init__(self) -> None:
        inherited = self.current.get()
        self.conditions: dict[QueryDependency, Q | None] = self.EMPTY_CONDITIONS if inherited is None else inherited
        # The conditions are the enclosing execution's own until a dependency of this one is loaded.
        self.conditions_are_shared = True
        self.snapshot = Registries.current()
        self.encryption = FieldEncryption.get_binding()
        self.row_scopes = RowScopeBinding.current.get() or RowScopeBinding()
        #: The tokens restoring the caller's context, while the inputs are installed.
        self.tokens: tuple[Token[Any] | None, Token[Any] | None, Token[Any] | None, Token[Any] | None] | None = None

    def __enter__(self) -> None:
        """Installs changed inputs for one step; inherited bindings retain their separate scopes."""
        self.tokens = (
            self.current.set(self.conditions) if self.current.get() is not self.conditions else None,
            Registries.execution_snapshot.set(self.snapshot)
            if Registries.execution_snapshot.get() is not self.snapshot
            else None,
            FieldEncryption.binding.set(self.encryption)
            if FieldEncryption.binding.get() is not self.encryption
            else None,
            RowScopeBinding.current.set(self.row_scopes)
            if RowScopeBinding.current.get() is not self.row_scopes
            else None,
        )

    def __exit__(self, *exception_info: object) -> None:
        """Restores the caller's context, on every exit."""
        token, snapshot_token, encryption_token, scope_token = self.tokens  # type: ignore[misc]
        self.tokens = None
        if scope_token is not None:
            RowScopeBinding.current.reset(scope_token)
        elif RowScopeBinding.current.get() is not self.row_scopes:
            RowScopeBinding.current.set(self.row_scopes)
        if encryption_token is not None:
            FieldEncryption.binding.reset(encryption_token)
        elif FieldEncryption.binding.get() is not self.encryption:
            FieldEncryption.binding.set(self.encryption)
        if snapshot_token is not None:
            Registries.execution_snapshot.reset(snapshot_token)
        elif Registries.execution_snapshot.get() is not self.snapshot:
            Registries.execution_snapshot.set(self.snapshot)
        if token is not None:
            self.current.reset(token)
        elif self.current.get() is not self.conditions:
            self.current.set(self.conditions)

    async def prepare(self, specification: QuerySpecification[Any]) -> None:
        """Load missing inputs; an empty query performs no dependency IO.

        None marks an input skipped for an empty query. It supplies a neutral condition
        when compiling that query, but a nonempty query sharing the input must load it.
        """
        for dependency in specification._execution_dependencies:
            if self.conditions.get(dependency) is None:
                if self.conditions_are_shared:
                    self.conditions = self.conditions.copy()
                    self.conditions_are_shared = False
                    if self.tokens is not None:
                        token = self.current.set(self.conditions)
                        if self.tokens[0] is None:
                            self.tokens = (token, *self.tokens[1:])
                self.conditions[dependency] = None if specification._is_none else await dependency.load()

    async def call(self, operation: Callable[[], Awaitable[TResult]]) -> TResult:
        """Run an operation with these inputs and restore the caller's context on every exit."""
        with self:
            return await operation()

    @classmethod
    async def run(cls, query: Any) -> Any:
        """Prepare the declaration's dependencies before the statement's normal executor."""
        execution = cls()
        if query._execution_dependencies:
            # A loader may install an ambient binding of its own. Restore the execution's
            # snapshot before invoking the operation, as on iterator resume.
            with execution:
                await execution.prepare(query)
        with execution:
            return await query._execute_bound()

    @classmethod
    async def run_operation(
        cls,
        specifications: tuple[QuerySpecification[Any], ...],
        operation: Callable[[], Awaitable[TResult]],
    ) -> TResult:
        """Prepare inputs shared by an operation involving multiple query declarations."""
        execution = cls()
        for specification in specifications:
            if specification._execution_dependencies:
                with execution:
                    await execution.prepare(specification)
        with execution:
            return await operation()

    @staticmethod
    async def read(
        specification: QuerySpecification[Any], connection: DatabaseClient, operation: Coroutine[Any, Any, TResult]
    ) -> TResult:
        """Runs a read of a summary - with the read checks of ``check_read_lock()`` and
        ``raise_read_error()`` the readers of rows make themselves."""
        try:
            QueryExecution.check_read_lock(specification, connection)
        except QueryError:
            operation.close()
            raise
        try:
            return await operation
        except Exception as error:
            QueryExecution.raise_read_error(connection, error)
            raise

    @staticmethod
    def check_read_lock(specification: QuerySpecification[Any], connection: DatabaseClient) -> None:
        """Refuses a ``select_for_update()`` read outside a transaction - its lock would be released at
        once.

        Args:
            specification: The query read.
            connection: The connection it reads on.

        Raises:
            QueryError: The query locks its rows and the connection isn't a transaction's.
        """
        if specification._select_for_update and not isinstance(connection, TransactionClient):
            raise QueryError(SELECT_FOR_UPDATE_OUTSIDE_TRANSACTION)

    @staticmethod
    def raise_read_error(connection: DatabaseClient, error: Exception) -> None:
        """Raises a driver's failure to decode a read's result as hare's error, when it is one.

        Args:
            connection: The connection read on.
            error: The failure.
        """
        read_error = connection.get_result_read_error(error)
        if read_error is not None:
            raise read_error from error

    @classmethod
    def get_condition(cls, dependency: QueryDependency) -> Q:
        """Read an execution input, rejecting synchronous preview of an unbound dependency."""
        conditions = cls.current.get()
        if conditions is None or dependency not in conditions:
            raise QueryError(DEPENDENCY_REQUIRES_EXECUTION)
        condition = conditions[dependency]
        return Q() if condition is None else condition

    @classmethod
    async def iterate(
        cls,
        specification: QuerySpecification[Any],
        iterator: AsyncGenerator[TResult],
    ) -> AsyncGenerator[TResult]:
        """Keep dependency values for an iterator, without leaking context across yields.

        Closing or cancelling the iterator closes its underlying cursor with the same inputs.
        The inner generator is closed even when loading a dependency fails.
        """
        execution = cls()
        try:
            if specification._execution_dependencies:
                with execution:
                    await execution.prepare(specification)
            while True:
                try:
                    with execution:
                        item = await anext(iterator)
                except StopAsyncIteration:
                    return
                yield item
        finally:
            with execution:
                await iterator.aclose()
