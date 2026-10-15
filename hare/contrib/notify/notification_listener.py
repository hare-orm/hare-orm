from __future__ import annotations

import asyncio
import contextlib
import contextvars
import inspect
from collections.abc import Awaitable, Callable, Sequence
from types import TracebackType
from typing import Any

from hare.contrib.notify.constants import (
    DEFAULT_MAX_BACKOFF_SECONDS,
    DEFAULT_RECONNECT_BACKOFF_SECONDS,
    HEALTH_CHECK_INTERVAL_SECONDS,
    MAX_BACKOFF_EXPONENT,
)
from hare.core.connections.connections import Connections
from hare.core.log import logger
from hare.exceptions import ConfigurationError
from hare.numbers.finite_numbers import FiniteNumbers


class NotificationListener:
    """A Postgres ``LISTEN`` subscription that reconnects with backoff whenever its dedicated
    connection dies. NOTIFY isn't persistent - a notification sent while disconnected is lost - so
    it only speeds up a durable source of truth such as polling. On a backend without LISTEN
    ``start()``/``run()`` log an ERROR and return.

    Args:
        connection_alias: The connection alias, resolved when listening starts.
        channel: The channel to listen on.
        callback: Called with each notification's payload on the event loop's thread, with the
            starting code's contextvars. An async one runs as a tracked task that ``stop()``
            cancels. Exceptions are logged.
        reconnect_attempts: How many consecutive failed (re)connects are retried before giving up;
            None retries forever. Reset once a connection passes a health check.
        backoff: The base of an exponential backoff (``backoff * 2**attempt`` seconds), or the
            delays one per consecutive failure - the last repeats.
        max_backoff_seconds: The longest delay; None for no bound.

    Raises:
        ConfigurationError: ``reconnect_attempts``, ``backoff`` or ``max_backoff_seconds`` is out of
            range.
    """

    def __init__(
        self,
        connection_alias: str,
        channel: str,
        callback: Callable[[str], Awaitable[None] | None],
        *,
        reconnect_attempts: int | None = None,
        backoff: float | Sequence[float] = DEFAULT_RECONNECT_BACKOFF_SECONDS,
        max_backoff_seconds: float | None = DEFAULT_MAX_BACKOFF_SECONDS,
    ) -> None:
        self._validate_options(reconnect_attempts, backoff, max_backoff_seconds)
        self.connection_alias = connection_alias
        self.channel = channel
        self.callback = callback
        self.reconnect_attempts = reconnect_attempts
        self.backoff: float | tuple[float, ...] = (
            float(backoff) if isinstance(backoff, int | float) else tuple(float(delay) for delay in backoff)
        )
        self.max_backoff_seconds = max_backoff_seconds

        self._stopping = False
        #: Set by stop() - wakes run() from a health-check or backoff pause at once.
        self._stop_requested = asyncio.Event()
        #: The task run() executes in - whether start() created it or a caller awaits run() itself.
        self._run_task: asyncio.Task[None] | None = None
        #: Whether start() created the task run() executes in - stop() cancels only a task of its
        #: own, never the task of a caller awaiting run() itself.
        self._owns_run_task = False
        #: Set once the first LISTEN attempt has finished (either way) - start() waits on it.
        self._first_attempt_finished = asyncio.Event()
        self._connection: Any = None
        self._callback_context: contextvars.Context | None = None
        #: Tasks running async callbacks - held so they can't be garbage-collected mid-flight.
        self._callback_tasks: set[asyncio.Future[Any]] = set()

    @staticmethod
    def _validate_options(
        reconnect_attempts: int | None, backoff: float | Sequence[float], max_backoff_seconds: float | None
    ) -> None:
        """Checks the constructor's retry options for type and range.

        Raises:
            ConfigurationError: If any option is out of range.
        """
        if reconnect_attempts is not None and (
            isinstance(reconnect_attempts, bool) or not isinstance(reconnect_attempts, int) or reconnect_attempts < 0
        ):
            raise ConfigurationError(f"reconnect_attempts must be None or an int >= 0, got {reconnect_attempts!r}")
        delays = [backoff] if isinstance(backoff, int | float) else list(backoff)
        if not delays or any(not FiniteNumbers.is_finite_number(delay) or delay < 0 for delay in delays):
            raise ConfigurationError(
                f"backoff must be a finite number >= 0 or a non-empty sequence of them, got {backoff!r}"
            )
        if max_backoff_seconds is not None and (
            not FiniteNumbers.is_finite_number(max_backoff_seconds) or max_backoff_seconds <= 0
        ):
            raise ConfigurationError(
                f"max_backoff_seconds must be None or a finite number > 0, got {max_backoff_seconds!r}"
            )

    @property
    def is_listening(self) -> bool:
        """Whether a LISTEN connection is currently open and alive."""
        return self._connection is not None and not self._connection.is_closed()

    async def start(self) -> None:
        """Starts listening in a background task and returns once the first LISTEN attempt has
        finished (connected or failed - a failure keeps retrying in the background). Idempotent.
        On a backend without LISTEN/NOTIFY support logs an ERROR and does nothing."""
        if self._run_task is not None and not self._run_task.done():
            return
        if self._get_listen_method() is None:
            return
        self._stopping = False
        self._stop_requested.clear()
        self._first_attempt_finished.clear()
        # Never through a transaction start() may be called in - it ends long before the listener.
        task = Connections.current().create_task_outside_transactions(self.run())
        self._run_task = task
        self._owns_run_task = True
        waiter = asyncio.create_task(self._first_attempt_finished.wait())
        try:
            await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()

    async def stop(self) -> None:
        """Stops listening: ends the listening loop, closes the LISTEN connection and cancels
        still-running async callbacks, waiting for all of it. The task ``start()`` created is
        cancelled; a ``run()`` a caller awaits itself returns, at once from a pause between
        checks or reconnects. Idempotent."""
        self._stopping = True
        self._stop_requested.set()
        run_task, self._run_task = self._run_task, None
        owns_run_task, self._owns_run_task = self._owns_run_task, False
        if run_task is not None and run_task is not asyncio.current_task() and not run_task.done():
            if owns_run_task:
                run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run_task
        callback_tasks = list(self._callback_tasks)
        for callback_task in callback_tasks:
            callback_task.cancel()
        for callback_task in callback_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await callback_task

    async def __aenter__(self) -> NotificationListener:
        await self.start()
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        await self.stop()

    def _get_listen_method(self) -> Callable[..., Awaitable[Any]] | None:
        """The connection's ``listen()`` method, or ``None`` (logged at ERROR) if it has none."""
        connection = Connections.current().get_non_transactional(self.connection_alias)
        listen = getattr(connection, "listen", None)
        if listen is None:
            logger.error(
                "NotificationListener: %s backend has no notification channels - not listening on channel %r",
                connection.dialect.name,
                self.channel,
            )
        return listen

    def _get_backoff_delay(self, attempt: int) -> float:
        """The delay before reconnect attempt number ``attempt`` (0-based)."""
        if isinstance(self.backoff, float):
            delay = self.backoff * 2 ** min(attempt, MAX_BACKOFF_EXPONENT)
        else:
            delay = self.backoff[min(attempt, len(self.backoff) - 1)]
        if self.max_backoff_seconds is not None:
            delay = min(delay, self.max_backoff_seconds)
        return delay

    async def run(self) -> None:
        """Runs the listen/watch/reconnect loop in the current task until ``stop()`` is called,
        the task is cancelled, or the reconnect budget is exhausted. ``start()`` runs this in a
        background task; await it directly to own the task yourself."""
        self._run_task = asyncio.current_task()
        self._stopping = False
        self._stop_requested.clear()
        # Captured here, in the task started by the caller, so callbacks run with the caller's
        # contextvars rather than whatever is ambient where the driver invokes them.
        self._callback_context = contextvars.copy_context()
        try:
            listen = self._get_listen_method()
            if listen is None:
                return
            attempt = 0
            while not self._stopping:
                connection: Any = None
                failure: str | None = None
                try:
                    connection = await listen(self.channel, self._on_driver_notify)
                    self._connection = connection
                    self._first_attempt_finished.set()
                    # The attempt counter only resets once the connection survives a health
                    # check: one that dies faster than that (idle-session timeout, a firewall
                    # killing it over and over) is as unhealthy as one that never connected.
                    survived_a_health_check = False
                    while not self._stopping and not connection.is_closed():
                        await self.pause(HEALTH_CHECK_INTERVAL_SECONDS)
                        survived_a_health_check = not self._stopping
                    if self._stopping or survived_a_health_check:
                        if not self._stopping:
                            logger.warning(
                                "NotificationListener: the listening connection for channel %r closed, reconnecting",
                                self.channel,
                            )
                        attempt = 0
                        continue
                    failure = "listener closed before becoming healthy"
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    failure = f"listening setup failed: {error}"
                finally:
                    self._connection = None
                    if connection is not None:
                        await self._close_connection(connection)
                self._first_attempt_finished.set()
                if self.reconnect_attempts is not None and attempt >= self.reconnect_attempts:
                    logger.error(
                        "NotificationListener: giving up on channel %r after %d attempts (%s)",
                        self.channel,
                        attempt + 1,
                        failure,
                    )
                    return
                delay = self._get_backoff_delay(attempt)
                logger.warning(
                    "NotificationListener: channel %r attempt %d failed (%s), retrying in %.2fs",
                    self.channel,
                    attempt + 1,
                    failure,
                    delay,
                )
                await self.pause(delay)
                attempt += 1
        finally:
            self._first_attempt_finished.set()

    async def pause(self, seconds: float) -> None:
        """Waits ``seconds``, or until ``stop()`` is called.

        Args:
            seconds: How long to wait.
        """
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop_requested.wait(), seconds)

    @staticmethod
    async def _close_connection(connection: Any) -> None:
        """Closes a LISTEN connection, logging instead of raising if it is already broken."""
        try:
            await connection.close()
        except Exception:
            logger.debug("NotificationListener: closing a listening connection failed", exc_info=True)

    def _on_driver_notify(self, connection: Any, pid: int, channel: str, payload: str) -> None:
        """The driver-level ``listen()`` callback - both drivers invoke it on the event loop's
        thread; dispatches to ``callback`` inside the captured caller context."""
        if self._stopping or self._callback_context is None:
            return
        self._callback_context.run(self._invoke_callback, payload)

    def _invoke_callback(self, payload: str) -> None:
        """Calls ``callback``, running an awaitable result as a tracked task."""
        try:
            result = self.callback(payload)
        except Exception:
            logger.exception("NotificationListener: callback for channel %r failed", self.channel)
            return
        if inspect.isawaitable(result):
            callback_task = asyncio.ensure_future(result)
            self._callback_tasks.add(callback_task)
            callback_task.add_done_callback(self._on_callback_task_done)

    def _on_callback_task_done(self, callback_task: asyncio.Future[Any]) -> None:
        """Forgets a finished async-callback task, logging its exception if it raised one."""
        self._callback_tasks.discard(callback_task)
        if callback_task.cancelled():
            return
        exception = callback_task.exception()
        if exception is not None:
            logger.error(
                "NotificationListener: async callback for channel %r failed", self.channel, exc_info=exception
            )
