from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.core.registries import Registries
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.query.queryset.calls_before_setup import CallsBeforeSetup

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.queryset.queryset import QuerySet
    from hare.query.statements.awaitable_query import AwaitableQuery
    from hare.sql.queries.builder.query_builder import QueryBuilder
from hare.query.queryset.extensions.query_set_extension_call import QuerySetExtensionCall

#: Applies a method's call to the query being built: ``(query builder, *args, **kwargs)`` -> the
#: query builder with the call applied.
ExtensionApply = Callable[..., "QueryBuilder"]


class QuerySetExtensions:
    """The QuerySet methods dialects registered. Calling one on a queryset records the call - before
    ``Hare.init()`` too; the query applies it when built for its connection, through that
    connection's dialect. A dialect that registered no implementation raises ``UnSupportedError``.
    """

    #: The implementation of each method by dialect name, by method name.
    registered: ClassVar[dict[str, dict[str, ExtensionApply]]] = {}

    @classmethod
    def register(cls, name: str, dialect_name: str, apply: ExtensionApply) -> None:
        """Adds a QuerySet method for one dialect.

        Args:
            name: The method's name.
            dialect_name: The dialect the implementation is for.
            apply: Applies a call to the query builder: ``(builder, *args, **kwargs) -> builder``.

        Raises:
            ConfigurationError: The name is private or one of QuerySet's own attributes.
        """
        # Local import: the queryset module imports this one.
        from hare.query.queryset.queryset import QuerySet

        if name.startswith("_") or hasattr(QuerySet, name):
            raise ConfigurationError(
                f"A dialect's QuerySet method can't be named {name!r} - QuerySet already has it, or it is private"
            )
        cls.registered.setdefault(name, {})[dialect_name] = apply
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
            if not queryset._model_is_set_up():
                return cast("QuerySet[Any]", CallsBeforeSetup.keep(queryset, name, args, kwargs))
            clone = queryset._clone()
            clone._extension_calls = (*clone._extension_calls, QuerySetExtensionCall(name, args, dict(kwargs)))
            return clone

        return call

    @classmethod
    def get_implementation(cls, query: AwaitableQuery[Any], extension_call: QuerySetExtensionCall) -> ExtensionApply:
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
        apply = implementations.get(dialect_name)
        if apply is None:
            raise UnSupportedError(
                f"{query.model.__name__}.{extension_call.name}() is a method of the "
                f"{', '.join(sorted(implementations)) or 'no longer registered'} dialect - the query runs on "
                f"{dialect_name}"
            )
        return apply

    @classmethod
    def apply(cls, query: AwaitableQuery[Any]) -> None:
        """Applies every recorded call to a query built for its connection.

        Args:
            query: The query, its ``query`` builder already built.

        Raises:
            UnSupportedError: The connection's dialect has no implementation of a called method.
        """
        for extension_call in query._extension_calls:
            apply = cls.get_implementation(query, extension_call)
            query.query = apply(query.query, *extension_call.args, **extension_call.kwargs)

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
