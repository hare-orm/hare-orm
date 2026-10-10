"""One registry of the observers of what the ORM does."""

from __future__ import annotations

import contextvars
import inspect
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Iterable
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from hare.core.log import db_client_logger, logger
from hare.dialects.base.constants import SLOW_QUERY_THRESHOLD_MS
from hare.instrumentation.declarations import QueryExecuted, TransactionEvent
from hare.instrumentation.observers.observer import Observer, ObserverCallback
from hare.instrumentation.observers.observer_dispatch import ObserverDispatch
from hare.instrumentation.observers.observer_set import ObserverSet
from hare.sql.terms.parameters.query_parameters import QueryParameters

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.instrumentation.declarations import QueryCall
    from hare.instrumentation.queries.query_wrapper import QueryWrapper
    from hare.models import Model
    from hare.transactions.enums import TransactionEventType

TCallback = TypeVar("TCallback", bound=ObserverCallback)


class Observers:
    """The observers of what the ORM does - one registry for every event:

    - ``QueryExecuted`` - every statement sent to the database;
    - ``TransactionEvent`` - every real, top-level begin, commit and rollback;
    - ``RowsChanged`` - the rows a committed write changed (see ``ChangeEvents``).

    ::

        Observers.observe(QueryExecuted, count_query)
        Observers.observe(RowsChanged, send_order_update, models=[Order, OrderLine])

    An observer is registered for the whole process (``observe()``), for one ``HareContext``
    (``HareContext.observe()`` - it gets the events while that context is the current one), or for
    the current context of the running code (``observing()`` - the block and the tasks it starts).

    A query or transaction event reaches a plain function right away, in the task that ran the
    query - keep it quick; an ``async def`` runs in the background (``ObserverDispatch``), never
    delaying the query. ``RowsChanged`` reaches every observer once the write's transaction
    commits, in the task that committed it, an ``async def`` awaited. An observer that raises is
    logged and never affects the query, the write or the other observers. Without observers an
    event costs nothing.

    ``wrap_queries()`` installs a ``QueryWrapper`` running around every query-executing call - a
    tracing span.
    """

    #: The observers of the whole process.
    process_observers: ClassVar[ObserverSet] = ObserverSet()

    #: The observers ``observing()`` installed in the current context - ``(event type, observer)``
    #: pairs, innermost last.
    context_observers: ClassVar[contextvars.ContextVar[tuple[tuple[type, Observer], ...]]] = contextvars.ContextVar(
        "hare_context_observers", default=()
    )

    #: The query wrappers, outermost first.
    query_wrappers: ClassVar[list[QueryWrapper]] = []

    #: A query taking at least this long is logged at DEBUG - set by
    #: ``Hare.init(slow_query_threshold_ms=...)``.
    slow_query_threshold_ms: ClassVar[float] = SLOW_QUERY_THRESHOLD_MS

    @classmethod
    def observe(
        cls, event_type: type, callback: TCallback, *, models: Iterable[type[Model]] | None = None
    ) -> TCallback:
        """Adds an observer of ``event_type`` for the whole process. Observing again adds the
        models to the ones it observes; an observer of every event stays one, and observing
        without models makes it one.

        Args:
            event_type: The event type.
            callback: The observer.
            models: For ``RowsChanged``: the models whose changes it gets - their subclasses'
                too; None for every model.

        Returns:
            The callback, to pass to ``unobserve()``.

        Raises:
            QueryError: ``models`` given for an event that isn't narrowed by model.
            TypeError: A model isn't a model class.
        """
        cls.process_observers.add(event_type, callback, models)
        return callback

    @classmethod
    def unobserve(
        cls, event_type: type, callback: ObserverCallback, *, models: Iterable[type[Model]] | None = None
    ) -> None:
        """Stops an observer of the whole process observing ``models`` - removes it entirely
        without them, or once it observes no model; nothing when it isn't registered.

        Args:
            event_type: The event type.
            callback: The observer.
            models: The models it stops observing.

        Raises:
            QueryError: The observer observes every model - it can only be removed entirely.
            TypeError: A model isn't a model class.
        """
        cls.process_observers.remove(event_type, callback, models)

    @classmethod
    @contextmanager
    def observing(
        cls, event_type: type, callback: ObserverCallback, *, models: Iterable[type[Model]] | None = None
    ) -> Generator[None]:
        """Observes ``event_type`` in the current context for the block - the code it runs and the
        tasks it starts.

        Args:
            event_type: The event type.
            callback: The observer.
            models: For ``RowsChanged``: the models whose changes it gets.

        Raises:
            QueryError, TypeError: See ``observe()``.
        """
        observer = Observer(callback, ObserverSet.get_model_set(event_type, models))
        token = cls.context_observers.set((*cls.context_observers.get(), (event_type, observer)))
        try:
            yield
        finally:
            try:
                cls.context_observers.reset(token)
            except ValueError:
                # Left in another context than entered (an async generator fixture finalized in
                # another task) - the observer is dropped from this one.
                cls.context_observers.set(
                    tuple(pair for pair in cls.context_observers.get() if pair[1] is not observer)
                )

    @classmethod
    def wrap_queries(cls, wrapper: QueryWrapper) -> None:
        """Installs a wrapper around every query-executing call - after the ones installed before;
        nothing when it is installed already.

        Args:
            wrapper: The wrapper.
        """
        if wrapper not in cls.query_wrappers:
            cls.query_wrappers.append(wrapper)

    @classmethod
    def unwrap_queries(cls, wrapper: QueryWrapper) -> None:
        """Removes a wrapper - nothing when it isn't installed.

        Args:
            wrapper: The wrapper.
        """
        if wrapper in cls.query_wrappers:
            cls.query_wrappers.remove(wrapper)

    @classmethod
    def get_observers(cls, event: Any) -> list[Observer]:
        """Every observer that gets ``event``: the process's, the current ``HareContext``'s and
        the current context's.

        Args:
            event: The event.

        Returns:
            The observers.
        """
        observers: list[Observer] = []
        if ObserverSet.total_count:
            observers.extend(cls.process_observers.get_observers(event))
            if (hare_context_observers := cls.get_hare_context_observers()) is not None:
                observers.extend(hare_context_observers.get_observers(event))
        for event_type, observer in cls.context_observers.get():
            if event_type is type(event) and observer.observes(event):
                observers.append(observer)
        return observers

    @classmethod
    def sees_query_parameters(cls) -> bool:
        """Whether anything is given the bound parameters of a query: a query wrapper, an observer,
        or the client log at DEBUG, which logs each statement with its parameters.

        Returns:
            True when something is.
        """
        return bool(
            cls.query_wrappers
            or ObserverSet.total_count
            or cls.context_observers.get()
            or db_client_logger.isEnabledFor(logging.DEBUG)
        )

    @classmethod
    def is_observed(cls, event_type: type, model: type[Model] | None = None) -> bool:
        """Whether an observer gets events of ``event_type`` - of ``model``, when given.

        Args:
            event_type: The event type.
            model: The model of the events.

        Returns:
            True when such an observer exists.
        """
        if ObserverSet.total_count:
            if cls.process_observers.observes(event_type, model):
                return True
            hare_context_observers = cls.get_hare_context_observers()
            if hare_context_observers is not None and hare_context_observers.observes(event_type, model):
                return True
        context_observers = cls.context_observers.get()
        return bool(context_observers) and any(
            observed_type is event_type and (model is None or observer.observes_model(model))
            for observed_type, observer in context_observers
        )

    @staticmethod
    def get_hare_context_observers() -> ObserverSet | None:
        """The observers of the current ``HareContext``, None without one."""
        # Deferred: the context imports the instrumentation package.
        from hare.core.hare_context import HareContext

        hare_context = HareContext.get_current()
        return None if hare_context is None else hare_context.observers

    @classmethod
    def notify(cls, event: Any) -> None:
        """Gives a query or transaction event to its observers: a plain function right away, an
        ``async def`` in the background. An event of a query an observer runs itself reaches no
        observer.

        Args:
            event: The event.
        """
        if ObserverDispatch.inside_observer.get():
            return
        background_observers: list[Observer] = []
        for observer in cls.get_observers(event):
            if observer.is_async:
                background_observers.append(observer)
                continue
            try:
                observer.callback(event)
            except Exception:
                logger.exception("Observer %r raised on %r", observer.callback, event)
        if background_observers:
            ObserverDispatch.schedule(background_observers, event)

    @classmethod
    async def deliver(cls, event: Any) -> None:
        """Gives an event to its observers in the running task, awaiting an ``async def`` one.

        Args:
            event: The event.
        """
        for observer in cls.get_observers(event):
            try:
                result = observer.callback(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("Observer %r raised on %r", observer.callback, event)

    @classmethod
    def record_query(
        cls,
        sql: str | None,
        parameters: list[Any] | None,
        start_time: float,
        error: Exception | None,
        connection_alias: str,
    ) -> None:
        """Reports a finished query-executing call: logs it at DEBUG when slow, and gives its
        ``QueryExecuted`` to the observers.

        Args:
            sql: The statement text.
            parameters: The bound parameters.
            start_time: ``time.perf_counter()`` before the call.
            error: The exception it ended with, None on success.
            connection_alias: The connection it ran on.
        """
        duration_ms = (time.perf_counter() - start_time) * 1000
        if ObserverSet.total_count or cls.context_observers.get():
            # A sensitive field's value is shown as <hidden> - the slow query log below writes
            # the parameters through their repr(), which hides it too.
            shown_parameters = parameters.get_shown() if isinstance(parameters, QueryParameters) else parameters
            cls.notify(QueryExecuted(sql or "", shown_parameters, duration_ms, error, connection_alias))
        if duration_ms >= cls.slow_query_threshold_ms:
            db_client_logger.debug("Slow query (%.1fms): %s: %s", duration_ms, sql, parameters)

    @classmethod
    def record_transaction(
        cls, event_type: TransactionEventType, connection_alias: str, duration_ms: float, error: Exception | None
    ) -> None:
        """Reports a real, top-level begin, commit or rollback.

        Args:
            event_type: Begin, commit or rollback.
            connection_alias: The connection.
            duration_ms: 0.0 for a begin, the time since the begin otherwise.
            error: The error a rollback was caused by.
        """
        if ObserverSet.total_count or cls.context_observers.get():
            cls.notify(TransactionEvent(event_type, connection_alias, duration_ms, error))

    @classmethod
    async def run_wrapped(cls, call: QueryCall, proceed: Callable[[], Awaitable[Any]]) -> Any:
        """Runs a query-executing call inside every query wrapper.

        Args:
            call: The call.
            proceed: Runs the call.

        Returns:
            What the call returned.
        """
        wrappers = list(cls.query_wrappers)

        async def run_from(index: int) -> Any:
            if index == len(wrappers):
                return await proceed()
            return await wrappers[index].around(call, lambda: run_from(index + 1))

        return await run_from(0)

    @classmethod
    def stream_wrapped(cls, call: QueryCall, proceed: Callable[[], AsyncIterator[Any]]) -> AsyncIterator[Any]:
        """Opens a stream inside every query wrapper.

        Args:
            call: The call.
            proceed: Opens the stream.

        Returns:
            The rows.
        """
        wrappers = list(cls.query_wrappers)

        def open_from(index: int) -> AsyncIterator[Any]:
            if index == len(wrappers):
                return proceed()
            return wrappers[index].around_stream(call, lambda: open_from(index + 1))

        return open_from(0)

    @staticmethod
    async def wait_for_pending(timeout_seconds: float | None = None) -> None:
        """Waits for the ``async def`` observers still running - see
        ``ObserverDispatch.wait_for_pending()``.

        Args:
            timeout_seconds: The wait bound, None to wait as long as they run.

        Raises:
            ValueError: ``timeout_seconds`` is not a positive number.
            Exception: What one background observer raised since the last call.
            ExceptionGroup: What two or more raised.
        """
        await ObserverDispatch.wait_for_pending(timeout_seconds)
