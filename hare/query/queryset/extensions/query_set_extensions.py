from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.core.registries import Registries
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.query.queryset.extensions.query_set_extension import QuerySetExtension
from hare.query.queryset.extensions.query_set_extension_call import QuerySetExtensionCall
from hare.query.queryset.extensions.query_set_extension_query import QuerySetExtensionQuery
from hare.query.queryset.pending_calls.calls_before_setup import CallsBeforeSetup

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
    from hare.query.queryset.query_specification import QuerySpecification
    from hare.query.queryset.queryset import QuerySet
    from hare.query.statements.awaitable_query import AwaitableQuery
    from hare.sql.builder.queries.query_builder import QueryBuilder

#: Applies a method's call to the query being built: ``(query builder, *args, **kwargs)`` - or
#: ``(query builder, QuerySetExtensionQuery, *args, **kwargs)`` - returns the query builder with the
#: call applied.
ExtensionApply = Callable[..., "QueryBuilder"]


class QuerySetExtensions:
    """The QuerySet methods dialects registered. Calling one on a queryset records the call - before
    ``Hare.init()`` too; the query applies it when built for its connection, through that
    connection's dialect. A dialect that registered no implementation raises ``UnSupportedError``.
    """

    #: The implementation of each method by dialect name, by method name.
    registered: ClassVar[dict[str, dict[str, QuerySetExtension]]] = {}

    @classmethod
    def register(
        cls,
        name: str,
        dialect_name: str,
        apply: ExtensionApply,
        *,
        reads_query: bool = False,
        takes_condition: bool = False,
        changes_rows: bool = False,
        read_result: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        """Adds a QuerySet method for one dialect.

        Args:
            name: The method's name.
            dialect_name: The dialect the implementation is for.
            apply: Applies a call to the query builder: ``(builder, *args, **kwargs) -> builder``,
                or ``(builder, extension_query, *args, **kwargs) -> builder`` with ``reads_query``.
            reads_query: Whether ``apply`` takes the ``QuerySetExtensionQuery`` of the query too.
            takes_condition: Whether the method's arguments are one condition, as ``filter()``'s -
                recorded as one ``Q``. Every dialect registering the name declares it alike.
            changes_rows: Whether a call changes which rows the query returns beyond its conditions
                - ``count()``/``exists()`` then count the rows returned.
            read_result: Reads the result of a query with the call:
                ``await read_result(extension_query, result, *args, **kwargs)``.

        Raises:
            ConfigurationError: The name is private or one of QuerySet's own attributes; another
                dialect registered it with another ``takes_condition``.
        """
        # Local import: the queryset module imports this one.
        from hare.query.queryset.queryset import QuerySet

        if name.startswith("_") or hasattr(QuerySet, name):
            raise ConfigurationError(
                f"A dialect's QuerySet method can't be named {name!r} - QuerySet already has it, or it is private"
            )
        implementations = cls.registered.setdefault(name, {})
        if any(
            implementation.takes_condition != takes_condition
            for registered_dialect_name, implementation in implementations.items()
            if registered_dialect_name != dialect_name
        ):
            raise ConfigurationError(
                f"The QuerySet method {name!r} is registered by another dialect with takes_condition="
                f"{not takes_condition} - its arguments are recorded alike for every dialect"
            )
        implementations[dialect_name] = QuerySetExtension(
            apply, reads_query, takes_condition, changes_rows, read_result
        )
        Registries.changed()

    @classmethod
    def get_method(cls, queryset: QuerySet[Any], name: str) -> Callable[..., QuerySet[Any]]:
        """The bound method a registered name stands for on a queryset.

        Args:
            queryset: The queryset.
            name: The method's name.

        Returns:
            A function returning a clone of the queryset with the call recorded.
        """

        def call(*args: Any, **kwargs: Any) -> QuerySet[Any]:
            if any(implementation.takes_condition for implementation in cls.registered.get(name, {}).values()):
                # Local import: the conditions import the queryset package this module is part of.
                from hare.query.expressions.conditions.q import Q

                # One Q is recorded as it is - the same condition as its filters given as keywords.
                condition = args[0] if len(args) == 1 and not kwargs and isinstance(args[0], Q) else Q(*args, **kwargs)
                args, kwargs = (condition,), {}
            if not queryset._model_is_set_up():
                return cast("QuerySet[Any]", CallsBeforeSetup.keep(queryset, name, args, kwargs))
            clone = queryset._clone()
            clone._extension_calls = (*clone._extension_calls, QuerySetExtensionCall(name, args, dict(kwargs)))
            return clone

        return call

    @classmethod
    def get_implementation(
        cls, query: AwaitableQuery[Any], extension_call: QuerySetExtensionCall
    ) -> QuerySetExtension:
        """The implementation of a called method for the dialect of the query's connection.

        Args:
            query: The query.
            extension_call: The call.

        Returns:
            The implementation.

        Raises:
            UnSupportedError: The connection's dialect has no implementation of the method.
        """
        dialect_name = query.dialect.name
        implementations = cls.registered.get(extension_call.name, {})
        implementation = implementations.get(dialect_name)
        if implementation is None:
            raise UnSupportedError(
                f"{query.model.__name__}.{extension_call.name}() is a method of the "
                f"{', '.join(sorted(implementations)) or 'no longer registered'} dialect - the query runs on "
                f"{dialect_name}"
            )
        return implementation

    @classmethod
    def apply(
        cls, query: AwaitableQuery[Any], value_wrapper_references: RecordedValueReferences | None = None
    ) -> None:
        """Applies every recorded call to a query built for its connection.

        Args:
            query: The query, its ``query`` builder already built.
            value_wrapper_references: The list the query's value references are recorded into, None when
                none are.

        Raises:
            UnSupportedError: The connection's dialect has no implementation of a called method.
        """
        extension_query = QuerySetExtensionQuery(query, value_wrapper_references)
        for extension_call in query._extension_calls:
            implementation = cls.get_implementation(query, extension_call)
            query.query = implementation.apply_call(
                query.query, extension_query, extension_call.args, extension_call.kwargs
            )

    @classmethod
    async def read_result(cls, query: AwaitableQuery[Any], result: Any) -> Any:
        """The result of a query with calls - each call's ``read_result`` reads it in turn.

        Args:
            query: The query, run.
            result: What it read.

        Returns:
            What the query returns.
        """
        extension_query = QuerySetExtensionQuery(query, None)
        for extension_call in query._extension_calls:
            read_result = cls.get_implementation(query, extension_call).read_result
            if read_result is not None:
                result = await read_result(extension_query, result, *extension_call.args, **extension_call.kwargs)
        return result

    @classmethod
    def reads_results(cls, query: QuerySpecification[Any]) -> bool:
        """Whether a call of the query reads its result (``QuerySetExtension.read_result``) on some
        dialect.

        Args:
            query: The query or queryset.

        Returns:
            True when one does.
        """
        return any(
            implementation.read_result is not None
            for extension_call in query._extension_calls
            for implementation in cls.registered.get(extension_call.name, {}).values()
        )

    @classmethod
    def changes_rows(cls, query: QuerySpecification[Any]) -> bool:
        """Whether a call of the query changes which rows it returns beyond its conditions
        (``QuerySetExtension.changes_rows``) on some dialect.

        Args:
            query: The query or queryset.

        Returns:
            True when one does.
        """
        return any(
            implementation.changes_rows
            for extension_call in query._extension_calls
            for implementation in cls.registered.get(extension_call.name, {}).values()
        )

    @classmethod
    def check(cls, query: AwaitableQuery[Any]) -> None:
        """Checks that the dialect of the query's connection implements every called method - for
        a query run on its plan's SQL text, which holds the calls already.

        Args:
            query: The query.

        Raises:
            UnSupportedError: The connection's dialect has no implementation of a called method.
        """
        for extension_call in query._extension_calls:
            cls.get_implementation(query, extension_call)
