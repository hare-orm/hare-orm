from __future__ import annotations

import dataclasses
import functools
import os
import sys
import types
from collections.abc import Callable
from copy import copy
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from hare.core.constants import HARE_PACKAGE_DIRECTORY
from hare.exceptions import ConfigurationError, QueryError
from hare.query.enums import FilterMethod

if TYPE_CHECKING:
    from hare.core.apps.model_binding_graph import ModelBindingGraph
    from hare.core.hare_context import HareContext
    from hare.query.queryset.query_specification import QuerySpecification

P = ParamSpec("P")
T = TypeVar("T")
TQuery = TypeVar("TQuery", bound="QuerySpecification[Any]")


@dataclasses.dataclass(frozen=True, slots=True)
class CallsBeforeSetup:
    """Calls of an unbound template, replayed into a local copy for one explicit model graph.

    Application initialization never mutates templates. Binding and execution preserve the
    original chain, so another application can replay it independently.

    Attributes:
        root: The query the first kept call was made on.
        calls: Each call - its method, positional and keyword arguments - in order.
        location: Where in the application's code the last call was made - ``file:line``.
    """

    #: The unchanged root of the template chain.
    root: QuerySpecification[Any]
    calls: tuple[tuple[str, tuple[Any, ...], dict[str, Any]], ...]
    location: str

    @staticmethod
    def recorded(method: Callable[P, T]) -> Callable[P, T]:
        """Makes a query method of the chain keep its call while the query's model isn't set up.

        Args:
            method: The method - it returns a query.

        Returns:
            The method.
        """

        call_method: Callable[..., Any] = method

        @functools.wraps(method)
        def call_or_keep(query: Any, /, *args: Any, **kwargs: Any) -> Any:
            if query._model_is_set_up():
                return call_method(query, *args, **kwargs)
            return CallsBeforeSetup.keep(query, method.__name__, args, kwargs)

        return call_or_keep  # type: ignore[return-value]

    @staticmethod
    def recorded_result(method: Callable[P, T]) -> Callable[P, T]:
        """Makes a query method returning another query (``count()``, ``get()``) keep its call
        while the query's model isn't set up. Its result remains a template; execution or
        explicit binding replays the calls into a separate query in the selected graph.

        Args:
            method: The method - it returns a query.

        Returns:
            The method.
        """

        call_method: Callable[..., Any] = method

        @functools.wraps(method)
        def call_and_keep(query: Any, /, *args: Any, **kwargs: Any) -> Any:
            is_template = not query._model_is_set_up()
            if is_template and method.__name__ in FilterMethod:
                from hare.query.filters.resolution.filter_values import FilterValues

                kwargs = FilterValues.get_captured_filter_kwargs(kwargs)
            result = call_method(query, *args, **kwargs)
            if is_template:
                result._calls_before_setup = CallsBeforeSetup(
                    query, ((method.__name__, args, kwargs),), CallsBeforeSetup.get_caller_location()
                )
            return result

        return call_and_keep  # type: ignore[return-value]

    @classmethod
    def keep(
        cls, query: QuerySpecification[Any], method_name: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> QuerySpecification[Any]:
        """A copy of ``query`` keeping the call ``query.<method_name>(*args, **kwargs)``.

        Args:
            query: The query.
            method_name: The method.
            args: Its positional arguments.
            kwargs: Its keyword arguments.

        Returns:
            The copy.
        """
        previous = query._calls_before_setup
        kept = copy(query)
        if method_name in FilterMethod:
            from hare.query.filters.resolution.filter_values import FilterValues

            kwargs = FilterValues.get_captured_filter_kwargs(kwargs)
        call = (method_name, args, kwargs)
        location = cls.get_caller_location()
        if previous is None:
            kept._calls_before_setup = CallsBeforeSetup(query, (call,), location)
        else:
            kept._calls_before_setup = CallsBeforeSetup(previous.root, (*previous.calls, call), location)
        return kept

    @staticmethod
    def get_caller_location() -> str:
        """Where the application's code called into hare - the first frame outside the package.

        Returns:
            ``file:line``, empty when every frame is inside the package.
        """
        frame: types.FrameType | None = sys._getframe(1)
        while frame is not None:
            filename = frame.f_code.co_filename
            if not os.path.abspath(filename).startswith(HARE_PACKAGE_DIRECTORY + os.sep):
                return f"{filename}:{frame.f_lineno}"
            frame = frame.f_back
        return ""

    @classmethod
    def bind(cls, query: TQuery, context: HareContext | None, graph: ModelBindingGraph | None = None) -> TQuery:
        """Replay an unbound declaration into an execution-local copy, preserving the template."""
        if query._binding is not None:
            if query._context is not context or (graph is not None and query._binding.graph is not graph):
                raise QueryError("Queries from different model graphs or contexts cannot be combined")
            return query
        if graph is None:
            if context is None:
                raise ConfigurationError("Binding a query needs an explicit graph or application context")
            apps = context.apps
            if apps is None:
                raise ConfigurationError("Binding a query needs an initialized model registry")
            graph = apps.binding_graph
        kept = query._calls_before_setup
        if kept is None:
            applied = copy(query)
            applied._context = context
            applied._binding = graph.get_binding(query.model)
            return applied
        applied = cls.bind(kept.root, context, graph)  # type: ignore[assignment]
        for method_name, args, kwargs in kept.calls:
            applied = getattr(applied, method_name)(*args, **kwargs)
        return applied
