from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, cast

from hare.core.extensions.declarations import ExtensionContribution, MethodRegistration
from hare.core.registries import Registries
from hare.exceptions import QueryError, UnSupportedError
from hare.query.queryset.constants import WRITE_REFUSED_EXTENSION_MESSAGE
from hare.query.queryset.extensions.declarations import BoundExtensionCall
from hare.query.queryset.extensions.enums import ExtensionPhase
from hare.query.queryset.extensions.query_set_extension import QuerySetExtension
from hare.query.queryset.extensions.query_set_extension_call import QuerySetExtensionCall
from hare.query.queryset.extensions.query_set_extension_query import QuerySetExtensionQuery
from hare.query.queryset.extensions.query_set_extension_result import QuerySetExtensionResult
from hare.query.queryset.pending_calls.calls_before_setup import CallsBeforeSetup

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
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

    #: The implementation of each method by dialect class, by method name.
    @staticmethod
    def get_registered() -> Mapping[str, Mapping[type[Dialect], QuerySetExtension]]:
        """The methods in the currently published generation."""
        return Registries.current().methods

    @classmethod
    def register(
        cls,
        name: str,
        dialect_class: type[Dialect],
        apply: ExtensionApply,
        *,
        reads_query: bool = False,
        takes_condition: bool = False,
        changes_rows: bool = False,
        transform_result: Callable[..., Any] | None = None,
        refuses_writes: bool = False,
        phase: ExtensionPhase = ExtensionPhase.ROW_SOURCE,
    ) -> None:
        """Adds a QuerySet method for one dialect.

        Args:
            name: The method's name.
            dialect_class: The dialect the implementation is for - each of its objects alike.
            apply: Applies a call to the query builder: ``(builder, *args, **kwargs) -> builder``,
                or ``(builder, extension_query, *args, **kwargs) -> builder`` with ``reads_query``.
            reads_query: Whether ``apply`` takes the ``QuerySetExtensionQuery`` of the query too.
            takes_condition: Whether the method's arguments are one condition, as ``filter()``'s -
                recorded as one ``Q``. Every dialect registering the name declares it alike.
            changes_rows: Whether a call changes which rows the query returns beyond its conditions
                - ``count()``/``exists()`` then count the rows returned.
            transform_result: Shapes an already executed QuerySetExtensionResult.
            phase: Whether SQL applies before derived wrapping or on the final statement.
            refuses_writes: Whether a write (``update()``, ``delete()``) refuses the call.

        Raises:
            ConfigurationError: The name is private or one of QuerySet's own attributes; another
                dialect registered it with another ``takes_condition``.
        """
        Registries.register(
            ExtensionContribution(
                methods=(
                    MethodRegistration(
                        name,
                        dialect_class,
                        QuerySetExtension(
                            apply,
                            reads_query,
                            takes_condition,
                            changes_rows,
                            transform_result,
                            refuses_writes,
                            phase,
                        ),
                    ),
                )
            )
        )

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
            if any(implementation.takes_condition for implementation in cls.get_registered().get(name, {}).values()):
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
        cls, query: QuerySpecification[Any], extension_call: QuerySetExtensionCall
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
        implementations = cls.get_registered().get(extension_call.name, {})
        implementation = implementations.get(type(query.dialect))
        if implementation is None:
            dialect_names = sorted(str(dialect_class.name) for dialect_class in implementations)
            raise UnSupportedError(
                f"{query.model.__name__}.{extension_call.name}() is a method of the "
                f"{', '.join(dialect_names) or 'no longer registered'} dialect - the query runs on "
                f"{query.dialect.name}"
            )
        return implementation

    @classmethod
    def bind(cls, query: AwaitableQuery[Any]) -> tuple[BoundExtensionCall, ...]:
        """Bind declarations to the actual connection before choosing SQL behavior.

        Multiple result handlers are rejected rather than silently choosing one or
        assuming that unrelated result shapes can be composed.
        """
        calls = tuple(BoundExtensionCall(call, cls.get_implementation(query, call)) for call in query._extension_calls)
        handlers = [bound.call.name for bound in calls if bound.implementation.transform_result is not None]
        if len(handlers) > 1:
            raise QueryError(f"Only one result-shaping extension can be used: {', '.join(handlers)}")
        query._bound_extensions = calls
        return calls

    @classmethod
    def get_bound_calls(cls, query: AwaitableQuery[Any]) -> tuple[BoundExtensionCall, ...]:
        """The implementations selected for this execution, without consulting other dialects."""
        calls = query._bound_extensions
        return cls.bind(query) if calls is None else calls

    @classmethod
    def apply(
        cls,
        query: AwaitableQuery[Any],
        value_wrapper_references: RecordedValueReferences | None = None,
        *,
        phase: ExtensionPhase | None = None,
    ) -> None:
        """Apply the selected SQL calls at their declared phase, preserving call order."""
        extension_query = QuerySetExtensionQuery(query, value_wrapper_references)
        for bound in cls.get_bound_calls(query):
            implementation, call = bound.implementation, bound.call
            if phase is None or implementation.phase == phase:
                query.query = implementation.apply_call(query.query, extension_query, call.args, call.kwargs)

    @classmethod
    def transform_result(cls, query: AwaitableQuery[Any], result: Any) -> Any:
        """Shape the ordinary result without executing another SQL statement."""
        context = query._extension_result or QuerySetExtensionResult(result)
        for bound in cls.get_bound_calls(query):
            transform = bound.implementation.transform_result
            if transform is not None:
                return transform(context, *bound.call.args, **bound.call.kwargs)
        return result

    @classmethod
    def changes_rows(cls, query: AwaitableQuery[Any]) -> bool:
        """Whether a selected implementation changes the rows of this execution."""
        return any(bound.implementation.changes_rows for bound in cls.get_bound_calls(query))

    @classmethod
    def raise_if_refused_in_write(cls, query: QuerySpecification[Any]) -> None:
        """Rejects a write called with a method it refuses (``QuerySetExtension.refuses_writes``) -
        before it picks its rows, however it picks them.

        Args:
            query: The write.

        Raises:
            QueryError: The write has such a call.
            UnSupportedError: The connection's dialect has no implementation of a called method.
        """
        for extension_call in query._extension_calls:
            if cls.get_implementation(query, extension_call).refuses_writes:
                raise QueryError(WRITE_REFUSED_EXTENSION_MESSAGE.format(method_name=extension_call.name))

    @classmethod
    def check(cls, query: AwaitableQuery[Any]) -> None:
        """Check all implementations and result-handler composition before executing cached SQL."""
        cls.get_bound_calls(query)
