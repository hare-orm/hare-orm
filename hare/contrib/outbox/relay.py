from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import Awaitable, Callable
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from hare.contrib.notify import NotificationListener
from hare.contrib.outbox.constants import (
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_BATCH_SIZE,
    DEFAULT_MAX_DELIVERY_ATTEMPTS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    LISTEN_MAX_RECONNECT_ATTEMPTS,
    MAX_BACKOFF_BASE_SECONDS,
    MAX_BATCH_SIZE,
    MAX_DELIVERY_ATTEMPTS_LIMIT,
    MAX_POLL_INTERVAL_SECONDS,
)
from hare.contrib.outbox.exceptions import DeliveryError
from hare.contrib.outbox.models import OutboxEvent
from hare.core.connections import Connections
from hare.core.log import logger
from hare.exceptions import ConfigurationError
from hare.transactions.transactions import Transactions
from hare.utils import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.queryset import QuerySet

TOutboxEvent = TypeVar("TOutboxEvent", bound=OutboxEvent)


class OutboxRelay(Generic[TOutboxEvent]):
    """Reads unpublished ``OutboxEvent`` rows and delivers them through ``deliver``, marking them
    published. Polling is the source of truth; ``listen_channel`` adds LISTEN/NOTIFY for lower
    latency, and when it fails the relay logs an ERROR and keeps polling.
    ``Hare.close_connections()`` doesn't stop it - call ``stop()`` or use ``async with``.

    Args:
        model: The ``OutboxEvent`` subclass.
        deliver: Called once per claimed row; raising marks the row failed, returning marks it
            published.
        poll_interval_seconds: The delay between polls, in ``(0, MAX_POLL_INTERVAL_SECONDS]``.
        batch_size: The most rows claimed per poll, in ``1..MAX_BATCH_SIZE``.
        listen_channel: A Postgres channel to LISTEN on - something must NOTIFY it (``publish(...,
            notify_channel=...)``). Ignored with an ERROR on a backend without LISTEN.
        max_delivery_attempts: A row reaching this many attempts is no longer claimed -
            dead-lettered, not deleted. In ``1..MAX_DELIVERY_ATTEMPTS_LIMIT``.
        backoff_base_seconds: The base of the backoff between LISTEN reconnects, in ``[0,
            MAX_BACKOFF_BASE_SECONDS]``.

    Raises:
        ConfigurationError: An option has the wrong type or is out of range.
    """

    def __init__(
        self,
        model: type[TOutboxEvent],
        deliver: Callable[[TOutboxEvent], Awaitable[None]],
        *,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        listen_channel: str | None = None,
        max_delivery_attempts: int = DEFAULT_MAX_DELIVERY_ATTEMPTS,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
    ) -> None:
        self._validate_options(
            poll_interval_seconds, batch_size, listen_channel, max_delivery_attempts, backoff_base_seconds
        )
        self.model = model
        self.deliver = deliver
        self.poll_interval_seconds = poll_interval_seconds
        self.batch_size = batch_size
        self.listen_channel = listen_channel
        self.max_delivery_attempts = max_delivery_attempts
        self.backoff_base_seconds = backoff_base_seconds

        self._task: asyncio.Task[None] | None = None
        self._listen_task: asyncio.Task[None] | None = None
        #: Created by _run_listen_loop() - owns the LISTEN connection, its watchdog and reconnects.
        self._notification_listener: NotificationListener | None = None
        self._stopping = False
        #: Short-lived NOTIFY-triggered poll tasks scheduled from the sync LISTEN callback - held here
        #: (not just asyncio.create_task()'s bare return value) so they can't be garbage-collected
        #: mid-flight; each removes itself once done.
        self._background_tasks: set[asyncio.Task[Any]] = set()
        #: The one notify-triggered poll task allowed to run at a time.
        self._notify_poll_task: asyncio.Task[None] | None = None
        #: Set by a NOTIFY arriving while _notify_poll_task runs - it then makes one more pass.
        self._notify_poll_rerun_requested = False

    @staticmethod
    def _validate_options(
        poll_interval_seconds: float,
        batch_size: int,
        listen_channel: str | None,
        max_delivery_attempts: int,
        backoff_base_seconds: float,
    ) -> None:
        """Checks the constructor's options for type and range.

        Raises:
            ConfigurationError: If any option has the wrong type or is out of range.
        """
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, int | float)
            or not math.isfinite(poll_interval_seconds)
            or not 0 < poll_interval_seconds <= MAX_POLL_INTERVAL_SECONDS
        ):
            raise ConfigurationError(
                f"poll_interval_seconds must be a number > 0 and <= {MAX_POLL_INTERVAL_SECONDS}, "
                f"got {poll_interval_seconds!r}"
            )
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= MAX_BATCH_SIZE:
            raise ConfigurationError(f"batch_size must be an int in 1..{MAX_BATCH_SIZE}, got {batch_size!r}")
        if listen_channel is not None and (not isinstance(listen_channel, str) or not listen_channel):
            raise ConfigurationError(f"listen_channel must be None or a non-empty string, got {listen_channel!r}")
        if (
            isinstance(max_delivery_attempts, bool)
            or not isinstance(max_delivery_attempts, int)
            or not 1 <= max_delivery_attempts <= MAX_DELIVERY_ATTEMPTS_LIMIT
        ):
            raise ConfigurationError(
                f"max_delivery_attempts must be an int in 1..{MAX_DELIVERY_ATTEMPTS_LIMIT}, "
                f"got {max_delivery_attempts!r}"
            )
        if (
            isinstance(backoff_base_seconds, bool)
            or not isinstance(backoff_base_seconds, int | float)
            or not math.isfinite(backoff_base_seconds)
            or not 0 <= backoff_base_seconds <= MAX_BACKOFF_BASE_SECONDS
        ):
            raise ConfigurationError(
                f"backoff_base_seconds must be a number >= 0 and <= {MAX_BACKOFF_BASE_SECONDS}, "
                f"got {backoff_base_seconds!r}"
            )

    async def start(self) -> None:
        """Starts the relay's polling loop (and, if ``listen_channel`` is set, its LISTEN/NOTIFY
        subscription) as a background task. Idempotent - a second call on an already-started
        relay is a no-op."""
        if self._task is not None:
            return
        self._stopping = False
        # Never through a transaction start() may be called in - it ends long before the relay.
        self._task = Connections.current().create_task_outside_transactions(self._run())

    async def stop(self) -> None:
        """Stops the relay's background tasks and waits for them to finish cleanly. Idempotent -
        a second call, or one on a relay that was never started, is a no-op."""
        # Set before anything below ever awaits - _on_notify() is synchronous, so no invocation
        # of it that executes after this line can add a task to self._background_tasks, which
        # keeps the snapshot-then-cancel below complete even if a NOTIFY arrives mid-stop().
        self._stopping = True
        if self._task is not None:
            task, self._task = self._task, None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._listen_task is not None:
            listen_task, self._listen_task = self._listen_task, None
            listen_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listen_task
        if self._notification_listener is not None:
            notification_listener, self._notification_listener = self._notification_listener, None
            await notification_listener.stop()
        # A copy - each finished task removes itself from the set.
        background_tasks = list(self._background_tasks)
        for task in background_tasks:
            task.cancel()
        for task in background_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def __aenter__(self) -> OutboxRelay[TOutboxEvent]:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    async def _run(self) -> None:
        if self.listen_channel is not None:
            self._listen_task = asyncio.create_task(self._run_listen_loop())
        while not self._stopping:
            await self._poll_once_safely()
            await asyncio.sleep(self.poll_interval_seconds)

    def _base_queryset(self) -> QuerySet[TOutboxEvent]:
        """The queryset every relay query starts from - spanning every tenant: the relay serves the
        whole table, and no tenant scope is active around it.
        """
        if self.model._meta.tenant_field:
            return self.model.objects.all_tenants()
        return self.model.objects.all()

    async def _poll_once_safely(self) -> int:
        """``_poll_once()``, with any exception it raises caught and logged instead of killing
        the polling loop (or, when called from the LISTEN callback's own background task, being
        dropped as an "exception never retrieved" warning).

        Returns:
            The number of rows claimed, or 0 if the cycle failed.
        """
        try:
            return await self._poll_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("OutboxRelay: poll cycle for %s failed", self.model.__name__)
            return 0

    async def _poll_once(self) -> int:
        """Claims up to ``batch_size`` unpublished, not dead-lettered rows and delivers each. Where the
        database has ``SELECT ... FOR UPDATE``, each row is claimed with ``SKIP LOCKED`` and
        delivered in its own transaction - concurrent relays split the queue, and a failure never
        undoes an earlier row's delivery. On SQLite one relay is assumed: the batch is claimed in
        one query, without a transaction.

        Returns:
            The number of rows claimed.
        """
        db = self.model.get_connection(for_write=True)
        if db.features.supports_select_for_update:
            # The rows this poll failed to deliver - not claimed again until the next poll. A
            # delivered row is published, which the claim's own filter leaves out.
            failed_ids: list[Any] = []
            claimed_count = 0
            for _ in range(self.batch_size):
                claim_outcome = await self._claim_and_deliver_one(exclude_ids=failed_ids)
                if claim_outcome is None:
                    break
                claimed_count += 1
                claimed_id, delivered = claim_outcome
                if not delivered:
                    failed_ids.append(claimed_id)
            return claimed_count
        claimed = await (
            self._base_queryset()
            .filter(published_at=None, attempts__lt=self.max_delivery_attempts)
            .order_by("created_at")
            .limit(self.batch_size)
        )
        for event in claimed:
            await self._run_row_to_completion_once_delivered(partial(self._deliver_one, event))
        return len(claimed)

    @staticmethod
    async def _run_row_to_completion_once_delivered(
        run_row: Callable[[asyncio.Event], Awaitable[Any]],
    ) -> Any:
        """Runs one row's delivery, holding back a cancellation that arrives after ``deliver()``
        returned until the row's bookkeeping save has committed - a delivered row would otherwise be
        delivered again. A cancellation during ``deliver()`` cancels it.

        Args:
            run_row: The row's coroutine factory; it sets the given event once ``deliver()``
                returned or raised.

        Returns:
            What ``run_row`` returns.
        """
        delivery_finished = asyncio.Event()
        row_task = asyncio.ensure_future(run_row(delivery_finished))
        try:
            return await asyncio.shield(row_task)
        except asyncio.CancelledError:
            if delivery_finished.is_set():
                with contextlib.suppress(Exception):
                    await row_task
            else:
                row_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await row_task
            raise

    async def _claim_and_deliver_one(self, *, exclude_ids: list[Any]) -> tuple[Any, bool] | None:
        return await self._run_row_to_completion_once_delivered(
            lambda delivery_finished: self._claim_and_deliver_one_in_transaction(exclude_ids, delivery_finished)
        )

    async def _claim_and_deliver_one_in_transaction(
        self, exclude_ids: list[Any], delivery_finished: asyncio.Event
    ) -> tuple[Any, bool] | None:
        """Claims one row (``SELECT ... FOR UPDATE SKIP LOCKED``) and delivers it in a transaction of
        its own.

        Args:
            exclude_ids: The primary keys this poll failed to deliver.
            delivery_finished: Set once ``deliver()`` returned or raised.

        Returns:
            The claimed row's primary key and whether it was delivered, None once the queue is
            exhausted.
        """
        connection_name = self.model.get_connection(for_write=True).connection_name
        async with Transactions.atomic(using=connection_name):
            # Built inside the transaction block - the queryset takes the transaction's connection.
            queryset = self._base_queryset().filter(published_at=None, attempts__lt=self.max_delivery_attempts)
            if exclude_ids:
                queryset = queryset.filter(id__not_in=exclude_ids)
            claimed = await queryset.order_by("created_at").limit(1).select_for_update(skip_locked=True)
            if not claimed:
                return None
            event = claimed[0]
            delivered = await self._deliver_one(event, delivery_finished, savepoint_connection_name=connection_name)
        return event.id, delivered

    async def _deliver_one(
        self,
        event: TOutboxEvent,
        delivery_finished: asyncio.Event | None = None,
        *,
        savepoint_connection_name: str | None = None,
    ) -> bool:
        """Delivers one row and records the outcome on it.

        Args:
            event: The claimed row.
            delivery_finished: Set once ``deliver()`` has returned or raised.
            savepoint_connection_name: When set, ``deliver()`` runs inside a savepoint on this
                connection, so a database error it raises is rolled back to the savepoint and the
                enclosing claim transaction can still record ``attempts``/``last_error``.

        Returns:
            Whether the row was delivered.
        """
        try:
            if savepoint_connection_name is None:
                await self.deliver(event)
            else:
                async with Transactions.atomic(using=savepoint_connection_name):
                    await self.deliver(event)
                    # Before the savepoint release, so a stop() arriving during it waits for the
                    # bookkeeping save instead of cancelling an already-delivered row.
                    if delivery_finished is not None:
                        delivery_finished.set()
        except Exception as exc:
            if delivery_finished is not None:
                delivery_finished.set()
            delivery_error = DeliveryError(f"delivering outbox event {event.id} (topic={event.topic!r}) failed")
            delivery_error.__cause__ = exc
            event.attempts += 1
            event.last_error = str(exc)
            await event.save(update_fields=["attempts", "last_error"])
            if event.attempts >= self.max_delivery_attempts:
                logger.error(
                    "OutboxRelay: event %s (topic=%r) dead-lettered after %d attempts",
                    event.id,
                    event.topic,
                    event.attempts,
                    exc_info=delivery_error,
                )
            else:
                logger.warning(
                    "OutboxRelay: event %s (topic=%r) delivery failed (attempt %d/%d)",
                    event.id,
                    event.topic,
                    event.attempts,
                    self.max_delivery_attempts,
                    exc_info=delivery_error,
                )
            return False
        if delivery_finished is not None:
            delivery_finished.set()
        event.published_at = Timezone.now()
        await event.save(update_fields=["published_at"])
        return True

    async def cleanup_published(self, older_than: timedelta) -> int:
        """
        Deletes published rows older than ``older_than``. Not run automatically by ``start()`` -
        call it yourself at whatever cadence fits (e.g. from your own periodic task runner).

        Args:
            older_than: Age threshold - rows with ``published_at`` before ``now() - older_than``
                are deleted.

        Returns:
            Number of rows deleted.

        Raises:
            ConfigurationError: If the model has ``Meta.soft_delete_field`` set - QuerySet.delete()
                turns into a soft-delete UPDATE for such a model (see its own docstring), which
                would silently defeat this method's entire purpose (bounding the outbox table's
                real storage) with no error at all, most easily reached by inheriting
                Meta.soft_delete_field from the app's own shared base model.
        """
        if self.model._meta.soft_delete_field:
            raise ConfigurationError(
                f"{self.model.__name__} has Meta.soft_delete_field set - cleanup_published() needs "
                "a real DELETE to actually reclaim storage, but QuerySet.delete() on this model "
                "would silently soft-delete instead. Query and hard-delete these rows yourself, "
                "bypassing the soft-delete behavior deliberately, if you're sure that's safe here."
            )
        cutoff = Timezone.now() - older_than
        return await self._base_queryset().filter(published_at__lt=cutoff).delete()

    async def _run_listen_loop(self) -> None:
        """Runs this relay's ``NotificationListener`` on ``listen_channel`` in the current task,
        on the router-chosen write connection, until it gives up or the task is cancelled."""
        assert self.listen_channel is not None, "only ever scheduled when set, see _run()"  # nosec B101
        connection_name = self.model.get_connection(for_write=True).connection_name
        self._notification_listener = NotificationListener(
            connection_name,
            self.listen_channel,
            self._on_notify,
            reconnect_attempts=LISTEN_MAX_RECONNECT_ATTEMPTS,
            backoff=self.backoff_base_seconds,
            max_backoff_seconds=None,
        )
        await self._notification_listener.run()

    def _on_notify(self, payload: str) -> None:
        """The ``NotificationListener`` callback: schedules an extra poll as a tracked task, coalescing
        a burst of NOTIFYs into one running poll plus one more. Schedules nothing once ``stop()``
        started.
        """
        if self._stopping:
            return
        if self._notify_poll_task is not None and not self._notify_poll_task.done():
            # A notify-triggered poll is already running - ask it for one more pass instead of
            # starting another concurrent poll per NOTIFY.
            self._notify_poll_rerun_requested = True
            return
        self._notify_poll_rerun_requested = False
        task = asyncio.get_running_loop().create_task(self._run_notify_polls())
        self._notify_poll_task = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_notify_polls(self) -> None:
        """Runs poll cycles for NOTIFYs until a cycle neither saw a new NOTIFY arrive nor claimed
        a full batch."""
        while True:
            self._notify_poll_rerun_requested = False
            claimed_count = await self._poll_once_safely()
            if self._stopping:
                return
            if not self._notify_poll_rerun_requested and claimed_count < self.batch_size:
                return
