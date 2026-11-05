from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from hare.core.registries import Registries
from hare.exceptions import QueryError
from hare.fields.encrypted.field_encryption import FieldEncryption
from hare.query.execution.messages import DEPENDENCY_REQUIRES_EXECUTION
from hare.query.expressions import Q
from hare.query.scopes.row_scope_binding import RowScopeBinding

if TYPE_CHECKING:
    from hare.query.execution.query_dependency import QueryDependency
    from hare.query.queryset.query_specification import QuerySpecification
    from hare.query.statements.awaitable_query import AwaitableQuery

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

    __slots__ = ("conditions", "snapshot", "encryption", "row_scopes")

    def __init__(self) -> None:
        self.conditions: dict[QueryDependency, Q | None] = dict(self.current.get() or ())
        self.snapshot = Registries.current()
        self.encryption = FieldEncryption.get_binding()
        self.row_scopes = RowScopeBinding.current.get() or RowScopeBinding()

    async def prepare(self, specification: QuerySpecification[Any]) -> None:
        """Load missing inputs; an empty query performs no dependency IO.

        None marks an input skipped for an empty query. It supplies a neutral condition
        when compiling that query, but a nonempty query sharing the input must load it.
        """
        for dependency in specification._execution_dependencies:
            if self.conditions.get(dependency) is None:
                self.conditions[dependency] = None if specification._is_none else await dependency.load()

    async def call(self, operation: Callable[[], Awaitable[TResult]]) -> TResult:
        """Run an operation with these inputs and restore the caller's context on every exit."""
        token = self.current.set(self.conditions)
        snapshot_token = Registries.execution_snapshot.set(self.snapshot)
        encryption_token = FieldEncryption.binding.set(self.encryption)
        scope_token = RowScopeBinding.current.set(self.row_scopes)
        try:
            return await operation()
        finally:
            RowScopeBinding.current.reset(scope_token)
            FieldEncryption.binding.reset(encryption_token)
            Registries.execution_snapshot.reset(snapshot_token)
            self.current.reset(token)

    @classmethod
    async def run(cls, query: AwaitableQuery[Any]) -> Any:
        """Prepare the declaration's dependencies before the statement's normal executor."""
        execution = cls()
        await execution.call(lambda: execution.prepare(query))
        return await execution.call(query._execute_bound)

    @classmethod
    async def run_operation(
        cls,
        specifications: tuple[QuerySpecification[Any], ...],
        operation: Callable[[], Awaitable[TResult]],
    ) -> TResult:
        """Prepare inputs shared by an operation involving multiple query declarations."""
        execution = cls()
        for specification in specifications:
            await execution.call(lambda: execution.prepare(specification))
        return await execution.call(operation)

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
            await execution.call(lambda: execution.prepare(specification))
            while True:
                try:
                    item = await execution.call(lambda: anext(iterator))
                except StopAsyncIteration:
                    return
                yield item
        finally:
            await execution.call(iterator.aclose)
