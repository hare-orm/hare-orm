from __future__ import annotations

import dataclasses
import functools
import os
import sys
import types
from collections.abc import Callable
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, ParamSpec, TypeVar

from hare.core.constants import HARE_PACKAGE_DIRECTORY
from hare.exceptions import ConfigurationError, HareError
from hare.query.queryset.specification_copying import SpecificationCopying

if TYPE_CHECKING:
    from hare.query.queryset.query_specification import QuerySpecification

P = ParamSpec("P")
T = TypeVar("T")


@dataclasses.dataclass(frozen=True, slots=True)
class CallsBeforeSetup:
    """The calls made on a query before ``Hare.init()`` set its model up: such a call is only
    kept, and ``Hare.init()`` applies the kept calls of every such query once it has set the
    models up - the query is an ordinary one from then on.

    Attributes:
        root: The query the first kept call was made on.
        calls: Each call - its method, positional and keyword arguments - in order.
        location: Where in the application's code the last call was made - ``file:line``.
    """

    #: Every query keeping calls, until ``Hare.init()`` applies them.
    PENDING: ClassVar[list[QuerySpecification[Any]]] = []

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
            if query.model._meta._inited:
                return call_method(query, *args, **kwargs)
            return CallsBeforeSetup.keep(query, method.__name__, args, kwargs)

        return call_or_keep  # type: ignore[return-value]

    @staticmethod
    def recorded_result(method: Callable[P, T]) -> Callable[P, T]:
        """Makes a query method returning another query (``count()``, ``get()``) keep its call
        while the query's model isn't set up: the query it returns is built now, and
        ``Hare.init()`` builds it again from the query once the query's own kept calls applied.

        Args:
            method: The method - it returns a query.

        Returns:
            The method.
        """

        call_method: Callable[..., Any] = method

        @functools.wraps(method)
        def call_and_keep(query: Any, /, *args: Any, **kwargs: Any) -> Any:
            result = call_method(query, *args, **kwargs)
            if not query.model._meta._inited:
                result._calls_before_setup = CallsBeforeSetup(
                    query, ((method.__name__, args, kwargs),), CallsBeforeSetup.get_caller_location()
                )
                CallsBeforeSetup.PENDING.append(result)
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
        call = (method_name, args, kwargs)
        location = cls.get_caller_location()
        if previous is None:
            kept._calls_before_setup = CallsBeforeSetup(query, (call,), location)
        else:
            kept._calls_before_setup = CallsBeforeSetup(previous.root, (*previous.calls, call), location)
        cls.PENDING.append(kept)
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
    def apply_pending(cls) -> None:
        """Applies the kept calls of every query whose model is set up now, in place - called by
        ``Hare.init()`` once the models are set up. A query of a model not set up yet keeps them.

        Raises:
            ConfigurationError: Kept calls of some queries are invalid - each is named with
                where it was made. No pending query changes when preparation fails.
        """
        pending = list(cls.PENDING)
        previous_states = [(query, copy(query)) for query in pending]
        still_pending: list[QuerySpecification[Any]] = []
        errors: list[str] = []
        try:
            for query in pending:
                kept = query._calls_before_setup
                if kept is None:
                    continue
                if not query._model_is_set_up():
                    still_pending.append(query)
                    continue
                applied: Any = kept.root
                try:
                    for method_name, args, kwargs in kept.calls:
                        applied = getattr(applied, method_name)(*args, **kwargs)
                except HareError as error:
                    if not any(kept.location in line and str(error) in line for line in errors):
                        errors.append(f"  {kept.location}: {error}")
                    continue
                # Later result queries and nested expressions may refer to this query.
                # Expose its prepared state while completing the synchronous graph.
                SpecificationCopying.copy_state(applied, query, type(query))
            if errors:
                raise ConfigurationError("Queries built before Hare.init() are invalid:\n" + "\n".join(errors))
        except BaseException:
            for query, previous in previous_states:
                SpecificationCopying.copy_state(previous, query, type(query))
            cls.PENDING[:] = pending
            raise
        cls.PENDING[:] = still_pending
