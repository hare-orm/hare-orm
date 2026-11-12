from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import socket
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, ClassVar, Generic, TypeVar

from hare.contrib.outbox.constants import OUTBOX_KEY_MAX_LENGTH, OUTBOX_RELEASE_FAILED
from hare.contrib.outbox.deliveries.callable_delivery import CallableDelivery
from hare.contrib.outbox.deliveries.delivery_batch import DeliveryBatch
from hare.contrib.outbox.deliveries.delivery_result import DeliveryResult
from hare.contrib.outbox.exceptions import DeliveryError
from hare.contrib.outbox.outbox_event import OutboxEvent
from hare.contrib.outbox.relay.claimed_batch import ClaimedBatch
from hare.contrib.outbox.relay.constants import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_CLEANUP_BATCH_SIZE,
    DEFAULT_CONCURRENCY,
    DEFAULT_DELIVERY_TIMEOUT_SECONDS,
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_DELIVERY_ATTEMPTS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_RETRY_BASE_SECONDS,
    DEFAULT_RETRY_MAX_SECONDS,
    LAST_ERROR_MAX_LENGTH,
    LEASE_RENEWALS_PER_LEASE,
    MAX_RETRY_DELAY_EXPONENT,
    RETRY_JITTER_RATIO,
)
from hare.contrib.outbox.relay.declarations import OutboxDeadLettered, OutboxDelivered, OutboxDeliveryFailed
from hare.contrib.outbox.relay.outbox_backlog import OutboxBacklog
from hare.contrib.outbox.relay.outbox_store import OutboxStore
from hare.contrib.outbox.relay.topic_selection import TopicSelection
from hare.core.connections.connections import Connections
from hare.core.hare_context import HareContext
from hare.core.log import logger
from hare.core.resource_completion import ResourceCompletion
from hare.exceptions import ConfigurationError
from hare.instrumentation.observers.observers import Observers
from hare.time import Timezone

if TYPE_CHECKING:
    import datetime
    from types import TracebackType

    from hare.contrib.outbox.deliveries.outbox_delivery import OutboxDelivery
    from hare.contrib.outbox.wakeups.outbox_wakeup import OutboxWakeup
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.fields.relations.swappable_model_reference import SwappableModelReference

TOutboxEvent = TypeVar("TOutboxEvent", bound=OutboxEvent)


class OutboxRelay(Generic[TOutboxEvent]):
    """Delivers the events of an outbox model through ``delivery`` - at least once: a receiver
    deduplicates on ``event.id``.

    Each poll claims a batch with one short statement - the events still to deliver, due, not leased
    by a live relay, in ``sequence`` order: ``UPDATE ... SET lease_until, leased_by WHERE sequence IN
    (...) RETURNING``, the candidates ``FOR UPDATE SKIP LOCKED`` where the database locks rows, so
    several relays share the queue. The batch is delivered outside any transaction, and its outcome
    written with a separate update for each success carrying metadata and one shared update for
    the remaining successes. An event of an ordering key isn't claimed
    while an earlier event of the key is undelivered - a failed or dead-lettered one included - so a
    key's events are delivered strictly in order and a failure holds back only its own key. A failed
    event is tried again after ``min(retry_base_seconds * 2 ** (attempts - 1), retry_max_seconds)``,
    spread by up to 10% either way; after ``max_delivery_attempts`` it is dead-lettered
    (``dead_lettered_at``). A relay stopped between delivering and recording leaves its lease to
    expire - the batch is delivered again; a relay being stopped finishes its batch first.

    Polling is the source of truth; the wakeup - the model's ``Meta.outbox_wakeup`` by default - starts a
    poll as soon as events are written. ``Hare.close_connections()`` doesn't stop the relay - call
    ``stop()`` or use ``async with``.

    Args:
        model: The ``OutboxEvent`` subclass - its class, its ``"app.Model"`` label or a ``swappable()``
            setting; a label is looked up in the current context when the relay first needs the model.
        delivery: An ``OutboxDelivery``, or an async function of the event - raising fails it.
        poll_interval_seconds: The delay between polls without a signal.
        batch_size: The positive number of events claimed at most per poll.
        max_delivery_attempts: The attempts before an event is dead-lettered.
        retry_base_seconds: The pause after the first failure.
        retry_max_seconds: The longest pause.
        lease_seconds: How long a claimed batch belongs to this relay - longer than
            ``delivery_timeout_seconds``, else a lease running out mid-delivery lets another relay
            deliver the event too.
        delivery_timeout_seconds: The longest the delivery of an event (of a batch, for a delivery
            sending batches) may take.
        concurrency: The most events delivered at a time.
        topics: The topics of the events the relay delivers and cleans up - names, or prefix patterns
            ending in ``*`` (``shop.*``); every event when None.
        exclude_topics: Topics - names or prefix patterns - the relay leaves to other relays.
        wakeup: The wakeup the relay subscribes to - the model's ``Meta.outbox_wakeup`` when None.
        using: The client or connection alias for every claim, outcome and maintenance query.
            Selected once; background delivery uses its root client outside caller transactions.
        name: The relay's name in ``leased_by`` - host, process and a random part by default.

    Raises:
        ConfigurationError: ``batch_size`` is out of range, or a topic pattern has ``*`` or ``?``
            anywhere but a ``*`` at its end.
    """

    #: The random spread of the retry pauses.
    jitter_random: ClassVar[secrets.SystemRandom] = secrets.SystemRandom()

    def __init__(
        self,
        model: type[TOutboxEvent] | str | SwappableModelReference,
        delivery: OutboxDelivery | Callable[[TOutboxEvent], Awaitable[DeliveryResult | None]],
        *,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_delivery_attempts: int = DEFAULT_MAX_DELIVERY_ATTEMPTS,
        retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS,
        retry_max_seconds: float = DEFAULT_RETRY_MAX_SECONDS,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        delivery_timeout_seconds: float = DEFAULT_DELIVERY_TIMEOUT_SECONDS,
        concurrency: int = DEFAULT_CONCURRENCY,
        topics: Sequence[str] | None = None,
        exclude_topics: Sequence[str] = (),
        wakeup: OutboxWakeup | None = None,
        name: str | None = None,
        using: str | DatabaseClient | None = None,
    ) -> None:
        OutboxStore.check_count("batch_size", batch_size)
        #: The model as it was given - a label is looked up on first use.
        self.model_reference = model
        self._model: type[TOutboxEvent] | None = model if isinstance(model, type) else None
        self.using = using
        self._store: OutboxStore[TOutboxEvent] | None = None
        self.delivery = CallableDelivery.of(delivery)
        self.poll_interval_seconds = poll_interval_seconds
        self.batch_size = batch_size
        self.max_delivery_attempts = max_delivery_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.lease_seconds = lease_seconds
        self.delivery_timeout_seconds = delivery_timeout_seconds
        self.concurrency = concurrency
        self.topic_selection = TopicSelection(topics, exclude_topics)
        self._wakeup = wakeup
        self.name = name or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"[:OUTBOX_KEY_MAX_LENGTH]
        self.task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self.stopping = False
        #: Set by a wakeup signal - the loop polls at once instead of waiting out the interval.
        self.woken = asyncio.Event()

    @property
    def model(self) -> type[TOutboxEvent]:
        """The ``OutboxEvent`` subclass - a label given looked up in the current context once.

        Raises:
            ConfigurationError: The label names no model of the current context.
        """
        if self._model is None:
            self._model = HareContext.require_current().get_model(self.model_reference)  # type: ignore[assignment]
        return self._model  # type: ignore[return-value]

    @property
    def store(self) -> OutboxStore[TOutboxEvent]:
        """The storage bound on first use to a root client, outside caller transactions."""
        if self._store is None:
            client = Connections.get_client(self.using)
            if client is None:
                client = self.model.get_connection(for_write=True)
            if client.is_transaction_client:
                client = client.get_non_transactional_client()  # type: ignore[attr-defined]
            self._store = OutboxStore(self.model, client, topics=self.topic_selection)
        return self._store

    @property
    def wakeup(self) -> OutboxWakeup | None:
        """The wakeup the relay subscribes to - the model's ``Meta.outbox_wakeup`` when none was given."""
        return self._wakeup if self._wakeup is not None else self.model.get_wakeup()

    async def start(self) -> None:
        """Starts polling, and the wakeup subscription, as a background task. A second call is a
        no-op.

        Raises:
            ConfigurationError: The events' database can't claim them - the relay leases a batch
                with one ``UPDATE ... RETURNING`` holding back the events behind an undelivered one
                of their ordering key.
        """
        async with self._lifecycle_lock:
            if self._stop_task is not None:
                await ResourceCompletion.wait(self._stop_task)
            await self._start()

    async def _start(self) -> None:
        """Subscribe and publish one polling task while holding the lifecycle lock."""
        if self.task is not None:
            return
        connection = self.store.client
        features = connection.features
        if not (
            features.supports_row_updates and features.supports_returning and features.supports_correlated_subqueries
        ):
            raise ConfigurationError(
                f"The outbox relay of {self.model.__name__} leases its events with one UPDATE ... RETURNING "
                f"reading a correlated subquery, which the {connection.dialect.name} database of "
                f"{connection.connection_alias!r} doesn't run"
            )
        self.stopping = False
        wakeup = self.wakeup
        if wakeup is not None:
            try:
                await wakeup.subscribe(self.on_wakeup, connection)
            except BaseException as error:
                try:
                    await ResourceCompletion.wait(wakeup.unsubscribe(self.on_wakeup))
                except BaseException as release_error:
                    if release_error is error:
                        raise
                    raise BaseExceptionGroup(OUTBOX_RELEASE_FAILED, [error, release_error]) from None
                raise
        # Never through a transaction start() may be called in - it ends long before the relay.
        handler = connection.connection_handler or Connections.current()
        self.task = handler.create_task_outside_transactions(self.run())

    async def stop(self) -> None:
        """Stops the relay and waits for it, then closes the delivery's connections. A second
        call, or one on a relay never started, is a no-op. Concurrent calls wait for the same
        release. Cancellation is propagated only after the batch and all releases finish.
        An unsubscribe failure does not skip closing the delivery.
        """
        async with self._lifecycle_lock:
            if self._stop_task is None:
                if self.task is None:
                    return
                self.stopping = True
                self.woken.set()
                self._stop_task = asyncio.create_task(self._stop())
            task = self._stop_task
        try:
            await ResourceCompletion.wait(task)
        finally:
            if task.done() and self._stop_task is task:
                self._stop_task = None

    async def _stop(self) -> None:
        """Drain the poll and release wakeup and transport even when one release raises."""
        try:
            async with contextlib.AsyncExitStack() as releases:
                releases.push_async_callback(self.delivery.close)
                wakeup = self.wakeup
                if wakeup is not None:
                    releases.push_async_callback(wakeup.unsubscribe, self.on_wakeup)
                # stop() publishes one cleanup task only for a live poll, under the lifecycle lock.
                assert self.task is not None  # nosec B101
                self.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.task
        finally:
            self.task = None

    async def __aenter__(self) -> OutboxRelay[TOutboxEvent]:
        await self.start()
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        try:
            await self.stop()
        except BaseException as release_error:
            if exception is None or release_error is exception:
                raise
            raise BaseExceptionGroup(OUTBOX_RELEASE_FAILED, [exception, release_error]) from None

    def on_wakeup(self, topics: frozenset[str]) -> None:
        """The wakeup's callback: polls at once when the signal names a topic of this relay's, or
        none.

        Args:
            topics: The topics the signal names.
        """
        if not topics or any(self.topic_selection.takes(topic) for topic in topics):
            self.woken.set()

    async def run(self) -> None:
        """Polls until stopped - again at once while a poll claims events, else after a signal or
        the poll interval."""
        while not self.stopping:
            self.woken.clear()
            while not self.stopping and await self.poll_once_safely():
                pass
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.woken.wait(), self.poll_interval_seconds)

    async def poll_once_safely(self) -> int:
        """``poll_once()``, a failure logged instead of ending the loop.

        Returns:
            The number of events claimed, 0 when the poll failed.
        """
        try:
            return await self.poll_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("OutboxRelay: a poll of %s failed", self.model.__name__)
            return 0

    async def poll_once(self) -> int:
        """Claims a batch, delivers it and records the outcome.

        Returns:
            The number of events claimed.
        """
        batch = await self.claim()
        if not batch.events:
            return 0
        # Drain delivery and persistence before propagating cancellation. Persistence can take
        # longer than the delivery timeout; a failed acknowledgement still permits redelivery.
        await ResourceCompletion.wait(self.deliver_and_record(batch))
        return len(batch.events)

    async def deliver_and_record(self, batch: ClaimedBatch[TOutboxEvent]) -> None:
        """Delivers a claimed batch and records the outcome.

        Args:
            batch: The events and token of the claim.
        """
        renewal = asyncio.ensure_future(self.renew_lease(batch))
        try:
            outcomes = await self.deliver(batch.events)
        finally:
            renewal.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewal
        await self.record_outcomes(batch, outcomes)

    async def renew_lease(self, batch: ClaimedBatch[TOutboxEvent]) -> None:
        """Keeps the lease of a batch while it is delivered - renewed several times within its length,
        so a batch outlasting one lease isn't claimed and delivered again by another relay. Runs until
        cancelled; a renewal that fails is logged and tried again at the next one.

        Args:
            batch: The events and token of the claim.
        """
        while True:
            await asyncio.sleep(self.lease_seconds / LEASE_RENEWALS_PER_LEASE)
            try:
                await self.store.renew(batch, lease_seconds=self.lease_seconds)
            except Exception:
                logger.warning("OutboxRelay: renewing the lease of a batch failed", exc_info=True)

    async def claim(self) -> ClaimedBatch[TOutboxEvent]:
        """Claims the next due batch on the relay's selected connection."""
        return await self.store.claim(name=self.name, batch_size=self.batch_size, lease_seconds=self.lease_seconds)

    async def deliver(self, events: Sequence[TOutboxEvent]) -> list[DeliveryResult]:
        """Runs a transport batch and validates the event IDs of its outcomes."""
        return await DeliveryBatch.deliver(
            self.delivery,
            events,
            concurrency=self.concurrency,
            timeout_seconds=self.delivery_timeout_seconds,
        )

    def get_retry_delay_seconds(self, attempts: int) -> float:
        """The pause before an event is tried again.

        Args:
            attempts: Its failed attempts.

        Returns:
            The pause, spread at random.
        """
        # 2.0 ** 1024 overflows a float - a pause that long is the longest one anyway.
        growth = 2.0 ** min(attempts - 1, MAX_RETRY_DELAY_EXPONENT)
        delay = min(self.retry_base_seconds * growth, self.retry_max_seconds)
        return delay * (1 + self.jitter_random.uniform(-RETRY_JITTER_RATIO, RETRY_JITTER_RATIO))

    async def record_outcomes(self, batch: ClaimedBatch[TOutboxEvent], outcomes: Sequence[DeliveryResult]) -> None:
        """Persists outcomes by event ID, then reports the writes to observers.

        Args:
            batch: The claimed events whose delivery completed.
            outcomes: Exactly one result for each event in the batch.

        Raises:
            DeliveryError: The result count or event IDs do not match the claimed batch.
        """
        DeliveryBatch.check(batch.events, outcomes)
        now = Timezone.now()
        published = await self.store.acknowledge(batch, outcomes, now)
        events = {event.id: event for event in batch.events}
        for outcome in outcomes:
            if outcome.error is not None:
                await self.record_failure(batch, events[outcome.event_id], outcome, now)
        if published and Observers.is_observed(OutboxDelivered):
            for event in batch.events:
                if event.id not in published:
                    continue
                await Observers.deliver(
                    OutboxDelivered(
                        model=self.model,
                        event_id=event.id,
                        topic=event.topic,
                        attempts=event.attempts,
                        delay_seconds=(now - event.created_at).total_seconds(),
                    )
                )

    async def record_failure(
        self, batch: ClaimedBatch[TOutboxEvent], event: TOutboxEvent, result: DeliveryResult, now: datetime.datetime
    ) -> None:
        """Writes a failed delivery - the event to retry, or dead-lettered once out of attempts.

        Args:
            event: The event.
            batch: The events and token of the claim.
            result: What failed it and the attempt metadata.
            now: When the outcome is recorded.
        """
        error = result.error
        attempts = event.attempts + 1
        # A null byte or a lone surrogate - no text column stores either - is written escaped: refused,
        # the failure would never be recorded and the event leased again and again.
        error_text = (
            (str(error) or type(error).__name__)
            .encode("utf-8", "backslashreplace")
            .decode("utf-8")
            .replace("\x00", "\\x00")[:LAST_ERROR_MAX_LENGTH]
        )
        delivery_error = DeliveryError(f"delivering outbox event {event.id} (topic={event.topic!r}) failed")
        delivery_error.__cause__ = error
        dead_lettered = attempts >= self.max_delivery_attempts
        next_attempt_at = None if dead_lettered else now + timedelta(seconds=self.get_retry_delay_seconds(attempts))
        # A failure counts only while the event is still this relay's - one whose lease ran out may be
        # leased or published by another relay since.
        recorded = await self.store.record_failure(
            batch,
            result,
            attempts=attempts,
            error_text=error_text,
            next_attempt_at=next_attempt_at,
            dead_lettered_at=now if dead_lettered else None,
        )
        if not recorded:
            return
        if dead_lettered:
            logger.error(
                "OutboxRelay: event %s (topic=%r) dead-lettered after %d attempts",
                event.id,
                event.topic,
                attempts,
                exc_info=delivery_error,
            )
            if Observers.is_observed(OutboxDeadLettered):
                await Observers.deliver(
                    OutboxDeadLettered(
                        model=self.model, event_id=event.id, topic=event.topic, attempts=attempts, error=error_text
                    )
                )
            return
        logger.warning(
            "OutboxRelay: event %s (topic=%r) delivery failed (attempt %d/%d)",
            event.id,
            event.topic,
            attempts,
            self.max_delivery_attempts,
            exc_info=delivery_error,
        )
        if Observers.is_observed(OutboxDeliveryFailed):
            await Observers.deliver(
                OutboxDeliveryFailed(
                    model=self.model,
                    event_id=event.id,
                    topic=event.topic,
                    attempts=attempts,
                    error=error_text,
                    next_attempt_at=next_attempt_at,  # type: ignore[arg-type]
                )
            )

    async def get_backlog(self) -> OutboxBacklog:
        """The state of the relay's queue - for a health check.

        Returns:
            The events still to deliver, the age of the oldest of them, the dead-lettered events.
        """
        return await self.store.get_backlog()

    async def retry_dead_lettered(
        self, *, topics: Sequence[str] | None = None, ids: Sequence[uuid.UUID] | None = None
    ) -> int:
        """Gives dead-lettered events their attempts back - delivered again from the next poll.

        Args:
            topics: Only events of these topics - names, or prefix patterns ending in ``*``.
            ids: Only these events.

        Returns:
            How many events were given back.

        Raises:
            ConfigurationError: A topic pattern has ``*`` or ``?`` anywhere but a ``*`` at its end.
        """
        count = await self.store.retry_dead_lettered(topics=topics, ids=ids)
        if count:
            self.woken.set()
        return count

    async def cleanup_published(self, older_than: timedelta, *, batch_size: int = DEFAULT_CLEANUP_BATCH_SIZE) -> int:
        """Deletes events delivered more than ``older_than`` ago - batch by batch, never holding a long
        lock. Not run by the relay itself - call it at a cadence of your own.

        Args:
            older_than: The age of the events deleted.
            batch_size: The positive number of events deleted per statement.

        Returns:
            How many events were deleted.

        Raises:
            ConfigurationError: The model has ``Meta.soft_delete_field`` - its delete wouldn't free
                the rows; or ``batch_size`` is out of range.
        """
        return await self.store.cleanup_published(older_than, batch_size=batch_size)

    async def cleanup_dead_lettered(
        self, older_than: timedelta, *, batch_size: int = DEFAULT_CLEANUP_BATCH_SIZE
    ) -> int:
        """Deletes events dead-lettered more than ``older_than`` ago - batch by batch.

        Args:
            older_than: The age of the dead letters deleted.
            batch_size: The positive number of events deleted per statement.

        Returns:
            How many events were deleted.

        Raises:
            ConfigurationError: The model has ``Meta.soft_delete_field``, or ``batch_size`` is out
                of range.
        """
        return await self.store.cleanup_dead_lettered(older_than, batch_size=batch_size)
