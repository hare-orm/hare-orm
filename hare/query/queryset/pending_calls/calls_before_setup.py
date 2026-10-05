from __future__ import annotations

import dataclasses
import functools
import os
import sys
import types
from collections.abc import Callable
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, ParamSpec, TypeVar, cast

from hare.exceptions import ConfigurationError, HareError
from hare.query.queryset.options.query_options import QueryOptions
from hare.query.queryset.specification_copying import SpecificationCopying

if TYPE_CHECKING:  # pragma: nocoverage
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
        error: Why applying the calls failed - the query raises it when it is used.
    """

    #: Every query keeping calls, until ``Hare.init()`` applies them.
    PENDING: ClassVar[list[QuerySpecification[Any]]] = []
    #: The directory of the hare package - frames inside it aren't the application's code.
    PACKAGE_DIRECTORY: ClassVar[str] = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    root: QuerySpecification[Any]
    calls: tuple[tuple[str, tuple[Any, ...], dict[str, Any]], ...]
    location: str
    error: HareError | None = None

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
            # Kept calls are a query option - a query with default options keeps none.
            if query._options is not QueryOptions.DEFAULT:
                CallsBeforeSetup.raise_if_invalid(query)
            if query.model._meta._inited:
                return call_method(query, *args, **kwargs)
            return CallsBeforeSetup.keep(query, method.__name__, args, kwargs)

        return cast("Callable[P, T]", call_or_keep)

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
            # Kept calls are a query option - a query with default options keeps none.
            if query._options is not QueryOptions.DEFAULT:
                CallsBeforeSetup.raise_if_invalid(query)
            result = call_method(query, *args, **kwargs)
            if not query.model._meta._inited:
                result._calls_before_setup = CallsBeforeSetup(
                    query, ((method.__name__, args, kwargs),), CallsBeforeSetup.get_caller_location()
                )
                CallsBeforeSetup.PENDING.append(result)
            return result

        return cast("Callable[P, T]", call_and_keep)

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
    def raise_if_invalid(query: QuerySpecification[Any]) -> None:
        """Raises why the calls kept on ``query`` before ``Hare.init()`` couldn't be applied.

        Args:
            query: The query.

        Raises:
            HareError: The error applying them raised.
        """
        kept = query._calls_before_setup
        if kept is not None and kept.error is not None:
            raise kept.error

    @classmethod
    def get_caller_location(cls) -> str:
        """Where the application's code called into hare - the first frame outside the package.

        Returns:
            ``file:line``, empty when every frame is inside the package.
        """
        frame: types.FrameType | None = sys._getframe(1)
        while frame is not None:
            filename = frame.f_code.co_filename
            if not os.path.abspath(filename).startswith(cls.PACKAGE_DIRECTORY + os.sep):
                return f"{filename}:{frame.f_lineno}"
            frame = frame.f_back
        return ""

    @classmethod
    def apply_pending(cls) -> None:
        """Applies the kept calls of every query whose model is set up now, in place - called by
        ``Hare.init()`` once the models are set up. A query of a model not set up yet keeps them.

        Raises:
            ConfigurationError: Kept calls of some queries are invalid - each is named with
                where it was made; such a query raises its error when it is used.
        """
        still_pending: list[QuerySpecification[Any]] = []
        errors: list[str] = []
        for query in cls.PENDING:
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
                query._calls_before_setup = dataclasses.replace(kept, error=error)
                if not any(kept.location in line and str(error) in line for line in errors):
                    errors.append(f"  {kept.location}: {error}")
                continue
            SpecificationCopying.compile_copy(type(query))(applied, query)
        cls.PENDING[:] = still_pending
        if errors:
            raise ConfigurationError("Queries built before Hare.init() are invalid:\n" + "\n".join(errors))
