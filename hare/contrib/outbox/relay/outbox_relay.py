from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import socket
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar

from hare.contrib.outbox.constants import MAX_INTERVAL_SECONDS, OUTBOX_KEY_MAX_LENGTH
from hare.contrib.outbox.deliveries.callable_delivery import CallableDelivery
from hare.contrib.outbox.exceptions import DeliveryError
from hare.contrib.outbox.outbox_event import OutboxEvent
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
    MAX_BATCH_SIZE,
    MAX_CONCURRENCY,
    MAX_DELIVERY_ATTEMPTS_LIMIT,
    RETRY_JITTER_RATIO,
)
from hare.contrib.outbox.relay.declarations import OutboxDeadLettered, OutboxDelivered, OutboxDeliveryFailed
from hare.contrib.outbox.relay.outbox_backlog import OutboxBacklog
from hare.core.connections.connections import Connections
from hare.core.log import logger
from hare.exceptions import ConfigurationError
from hare.instrumentation.observers.observers import Observers
from hare.numbers.finite_numbers import FiniteNumbers
from hare.query.expressions import Exists, OuterReference, Q
from hare.time import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    import datetime
    from types import TracebackType

    from hare.contrib.outbox.deliveries.outbox_delivery import OutboxDelivery
    from hare.contrib.outbox.wakeups.outbox_wakeup import OutboxWakeup
    from hare.query.queryset import QuerySet

TOutboxEvent = TypeVar("TOutboxEvent", bound=OutboxEvent)


class OutboxRelay(Generic[TOutboxEvent]):
    """Delivers the events of an outbox model through ``delivery`` - at least once: a receiver
    deduplicates on ``event.id``.

    Each poll claims a batch with one short statement - the events still to deliver, due, not leased
    by a live relay, in ``sequence`` order: ``UPDATE ... SET lease_until, leased_by WHERE sequence IN
    (...) RETURNING``, the candidates ``FOR UPDATE SKIP LOCKED`` where the database locks rows, so
    several relays share the queue. The batch is delivered outside any transaction, and its outcome
    written with one statement for the delivered events. An event of an ordering key isn't claimed
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
        model: The ``OutboxEvent`` subclass.
        delivery: An ``OutboxDelivery``, or an async function of the event - raising fails it.
        poll_interval_seconds: The delay between polls without a signal, in ``(0, 86400]``.
        batch_size: The most events claimed per poll, in ``1..10000``.
        max_delivery_attempts: The attempts before an event is dead-lettered, in ``1..10000``.
        retry_base_seconds: The pause after the first failure, in ``[0, 86400]``.
        retry_max_seconds: The longest pause, in ``[retry_base_seconds, 86400]``.
        lease_seconds: How long a claimed batch belongs to this relay, in ``(0, 86400]`` - longer
            than ``delivery_timeout_seconds``.
        delivery_timeout_seconds: The longest the delivery of an event (of a batch, for a delivery
            sending batches) may take, in ``(0, 86400]``.
        concurrency: The most events delivered at a time, in ``1..1000``.
        topics: The topics of the events the relay delivers and cleans up - every event when None.
        wakeup: The wakeup the relay subscribes to - the model's ``Meta.outbox_wakeup`` when None.
        name: The relay's name in ``leased_by`` - host, process and a random part by default.

    Raises:
        ConfigurationError: An option has the wrong type or is out of range.
    """

    #: The random spread of the retry pauses.
    jitter_random: ClassVar[secrets.SystemRandom] = secrets.SystemRandom()

    def __init__(
        self,
        model: type[TOutboxEvent],
        delivery: OutboxDelivery | Callable[[TOutboxEvent], Awaitable[None]],
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
        wakeup: OutboxWakeup | None = None,
        name: str | None = None,
    ) -> None:
        self.check_seconds("poll_interval_seconds", poll_interval_seconds, allow_zero=False)
        self.check_count("batch_size", batch_size, MAX_BATCH_SIZE)
        self.check_count("max_delivery_attempts", max_delivery_attempts, MAX_DELIVERY_ATTEMPTS_LIMIT)
        self.check_seconds("retry_base_seconds", retry_base_seconds, allow_zero=True)
        self.check_seconds("retry_max_seconds", retry_max_seconds, allow_zero=True)
        if retry_max_seconds < retry_base_seconds:
            raise ConfigurationError(
                f"retry_max_seconds ({retry_max_seconds!r}) must be at least retry_base_seconds "
                f"({retry_base_seconds!r})"
            )
        self.check_seconds("lease_seconds", lease_seconds, allow_zero=False)
        self.check_seconds("delivery_timeout_seconds", delivery_timeout_seconds, allow_zero=False)
        if delivery_timeout_seconds >= lease_seconds:
            raise ConfigurationError(
                f"delivery_timeout_seconds ({delivery_timeout_seconds!r}) must be shorter than lease_seconds "
                f"({lease_seconds!r}) - a lease running out mid-delivery lets another relay deliver the event too"
            )
        self.check_count("concurrency", concurrency, MAX_CONCURRENCY)
        if topics is not None and (
            isinstance(topics, str)
            or not isinstance(topics, Sequence)
            or not topics
            or not all(isinstance(topic, str) and topic for topic in topics)
        ):
            raise ConfigurationError(f"topics must be None or a non-empty sequence of topic names, got {topics!r}")
        if name is not None and (not isinstance(name, str) or not 1 <= len(name) <= OUTBOX_KEY_MAX_LENGTH):
            raise ConfigurationError(f"name must be None or a string of 1..{OUTBOX_KEY_MAX_LENGTH} characters")
        self.model = model
        self.delivery = CallableDelivery.of(delivery)
        self.poll_interval_seconds = poll_interval_seconds
        self.batch_size = batch_size
        self.max_delivery_attempts = max_delivery_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.lease_seconds = lease_seconds
        self.delivery_timeout_seconds = delivery_timeout_seconds
        self.concurrency = concurrency
        self.topics = tuple(topics) if topics is not None else None
        self.wakeup = wakeup if wakeup is not None else model.get_wakeup()
        self.name = name or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"[:OUTBOX_KEY_MAX_LENGTH]
        self.task: asyncio.Task[None] | None = None
        self.stopping = False
        #: Set by a wakeup signal - the loop polls at once instead of waiting out the interval.
        self.woken = asyncio.Event()

    @staticmethod
    def check_seconds(option_name: str, value: Any, *, allow_zero: bool) -> None:
        """Checks a duration option.

        Raises:
            ConfigurationError: It isn't a finite number in range.
        """
        if (
            not FiniteNumbers.is_finite_number(value)
            or not (value >= 0 if allow_zero else value > 0)
            or (value > MAX_INTERVAL_SECONDS)
        ):
            bound = ">= 0" if allow_zero else "> 0"
            raise ConfigurationError(
                f"{option_name} must be a number {bound} and <= {MAX_INTERVAL_SECONDS}, got {value!r}"
            )

    @staticmethod
    def check_count(option_name: str, value: Any, maximum: int) -> None:
        """Checks a count option.

        Raises:
            ConfigurationError: It isn't an int in ``1..maximum``.
        """
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ConfigurationError(f"{option_name} must be an int in 1..{maximum}, got {value!r}")

    async def start(self) -> None:
        """Starts polling, and the wakeup subscription, as a background task. A second call is a
        no-op.

        Raises:
            ConfigurationError: The events' database can't claim them - the relay leases a batch
                with one ``UPDATE ... RETURNING`` holding back the events behind an undelivered one
                of their ordering key.
        """
        if self.task is not None:
            return
        connection = self.model.get_connection(for_write=True)
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
        if self.wakeup is not None:
            await self.wakeup.subscribe(self.on_wakeup, connection.connection_alias)
        # Never through a transaction start() may be called in - it ends long before the relay.
        self.task = Connections.current().create_task_outside_transactions(self.run())

    async def stop(self) -> None:
        """Stops the relay and waits for it, then closes the delivery's connections. A second
        call, or one on a relay never started, is a no-op."""
        self.stopping = True
        self.woken.set()
        if self.task is not None:
            task, self.task = self.task, None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            if self.wakeup is not None:
                await self.wakeup.unsubscribe(self.on_wakeup)
            await self.delivery.close()

    async def __aenter__(self) -> OutboxRelay[TOutboxEvent]:
        await self.start()
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        await self.stop()

    def on_wakeup(self, topics: frozenset[str]) -> None:
        """The wakeup's callback: polls at once when the signal names a topic of this relay's, or
        none.

        Args:
            topics: The topics the signal names.
        """
        if self.topics is None or not topics or not topics.isdisjoint(self.topics):
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

    def get_base_queryset(self) -> QuerySet[TOutboxEvent]:
        """The queryset every relay query starts from - every tenant's events: the relay serves the
        whole table, no tenant scope around it."""
        if self.model._meta.tenant_field:
            return self.model.objects.all_tenants()
        return self.model.objects.all()

    def get_topic_queryset(self) -> QuerySet[TOutboxEvent]:
        """The relay's events - of its ``topics``."""
        queryset = self.get_base_queryset()
        if self.topics is not None:
            queryset = queryset.filter(topic__in=self.topics)
        return queryset

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
        events = await self.claim()
        if not events:
            return 0
        # Delivered and recorded even when the relay is stopped meanwhile - stop() waits for the batch
        # (at most delivery_timeout_seconds), so no event it delivered is delivered again.
        batch_task = asyncio.ensure_future(self.deliver_and_record(events))
        try:
            await asyncio.shield(batch_task)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await batch_task
            raise
        return len(events)

    async def deliver_and_record(self, events: list[TOutboxEvent]) -> None:
        """Delivers a claimed batch and records the outcome.

        Args:
            events: The events.
        """
        outcomes = await self.deliver(events)
        await self.record_outcomes(events, outcomes)

    async def claim(self) -> list[TOutboxEvent]:
        """Leases the next batch of due events to this relay.

        Returns:
            The events, in ``sequence`` order.
        """
        connection = self.model.get_connection(for_write=True)
        now = Timezone.now()
        earlier_undelivered = self.get_base_queryset().filter(
            ordering_key=OuterReference("ordering_key"),
            sequence__lt=OuterReference("sequence"),
            published_at__isnull=True,
        )
        candidates = (
            self.get_topic_queryset()
            .filter(published_at__isnull=True, dead_lettered_at__isnull=True)
            .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
            .filter(Q(lease_until__isnull=True) | Q(lease_until__lt=now))
            .annotate(held_back=Exists(earlier_undelivered))
            .filter(held_back=False)
            .order_by("sequence")
            .limit(self.batch_size)
        )
        if connection.features.supports_select_for_update:
            candidates = candidates.select_for_update(skip_locked=True)
        claimed = await (
            self.get_base_queryset()
            .using(connection)
            .filter(sequence__in=candidates.values("sequence"))
            .update(lease_until=now + timedelta(seconds=self.lease_seconds), leased_by=self.name)
            .returning()
        )
        return sorted(claimed, key=lambda event: event.sequence)

    async def deliver(self, events: list[TOutboxEvent]) -> list[BaseException | None]:
        """Delivers a batch.

        Args:
            events: The events - of distinct ordering keys.

        Returns:
            Per event, None when it was delivered, else what failed it.
        """
        try:
            outcomes = await self.delivery.deliver_batch(
                events, concurrency=self.concurrency, timeout_seconds=self.delivery_timeout_seconds
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return [error] * len(events)
        if len(outcomes) != len(events):
            error = DeliveryError(f"the delivery gave {len(outcomes)} outcomes for {len(events)} events")
            return [error] * len(events)
        return outcomes

    def get_retry_delay_seconds(self, attempts: int) -> float:
        """The pause before an event is tried again.

        Args:
            attempts: Its failed attempts.

        Returns:
            The pause, spread at random.
        """
        delay = min(self.retry_base_seconds * 2 ** (attempts - 1), self.retry_max_seconds)
        return delay * (1 + self.jitter_random.uniform(-RETRY_JITTER_RATIO, RETRY_JITTER_RATIO))

    async def record_outcomes(self, events: list[TOutboxEvent], outcomes: list[BaseException | None]) -> None:
        """Writes what became of a delivered batch - published, to retry or dead-lettered - and
        reports it to the observers.

        Args:
            events: The events.
            outcomes: Per event, None when it was delivered, else what failed it.
        """
        connection = self.model.get_connection(for_write=True)
        now = Timezone.now()
        delivered = [event for event, outcome in zip(events, outcomes, strict=True) if outcome is None]
        if delivered:
            await (
                self.get_base_queryset()
                .using(connection)
                .filter(sequence__in=[event.sequence for event in delivered])
                .update(published_at=now, lease_until=None, leased_by=None)
            )
        for event, outcome in zip(events, outcomes, strict=True):
            if outcome is not None:
                await self.record_failure(event, outcome, now)
        if delivered and Observers.is_observed(OutboxDelivered):
            for event in delivered:
                await Observers.deliver(
                    OutboxDelivered(
                        model=self.model,
                        event_id=event.id,
                        topic=event.topic,
                        attempts=event.attempts,
                        delay_seconds=(now - event.created_at).total_seconds(),
                    )
                )

    async def record_failure(self, event: TOutboxEvent, error: BaseException, now: datetime.datetime) -> None:
        """Writes a failed delivery - the event to retry, or dead-lettered once out of attempts.

        Args:
            event: The event.
            error: What failed it.
            now: When the outcome is recorded.
        """
        attempts = event.attempts + 1
        error_text = (str(error) or type(error).__name__)[:LAST_ERROR_MAX_LENGTH]
        delivery_error = DeliveryError(f"delivering outbox event {event.id} (topic={event.topic!r}) failed")
        delivery_error.__cause__ = error
        dead_lettered = attempts >= self.max_delivery_attempts
        next_attempt_at = None if dead_lettered else now + timedelta(seconds=self.get_retry_delay_seconds(attempts))
        await (
            self.get_base_queryset()
            .using(self.model.get_connection(for_write=True))
            .filter(sequence=event.sequence)
            .update(
                attempts=attempts,
                last_error=error_text,
                next_attempt_at=next_attempt_at,
                dead_lettered_at=now if dead_lettered else None,
                lease_until=None,
                leased_by=None,
            )
        )
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
        pending_queryset = self.get_topic_queryset().filter(published_at__isnull=True, dead_lettered_at__isnull=True)
        pending = await pending_queryset.count()
        oldest = await pending_queryset.order_by("sequence").first()
        dead_lettered = await self.get_topic_queryset().filter(dead_lettered_at__isnull=False).count()
        age_seconds = (Timezone.now() - oldest.created_at).total_seconds() if oldest is not None else 0.0
        return OutboxBacklog(pending=pending, oldest_pending_age_seconds=age_seconds, dead_lettered=dead_lettered)

    async def retry_dead_lettered(
        self, *, topics: Sequence[str] | None = None, ids: Sequence[uuid.UUID] | None = None
    ) -> int:
        """Gives dead-lettered events their attempts back - delivered again from the next poll.

        Args:
            topics: Only events of these topics.
            ids: Only these events.

        Returns:
            How many events were given back.
        """
        queryset = self.get_topic_queryset().filter(dead_lettered_at__isnull=False)
        if topics is not None:
            queryset = queryset.filter(topic__in=list(topics))
        if ids is not None:
            queryset = queryset.filter(id__in=list(ids))
        count = await queryset.update(dead_lettered_at=None, attempts=0, next_attempt_at=None)
        if count:
            self.woken.set()
        return count

    async def cleanup_published(self, older_than: timedelta, *, batch_size: int = DEFAULT_CLEANUP_BATCH_SIZE) -> int:
        """Deletes events delivered more than ``older_than`` ago - batch by batch, never holding a long
        lock. Not run by the relay itself - call it at a cadence of your own.

        Args:
            older_than: The age of the events deleted.
            batch_size: The events deleted per statement, in ``1..10000``.

        Returns:
            How many events were deleted.

        Raises:
            ConfigurationError: The model has ``Meta.soft_delete_field`` - its delete wouldn't free
                the rows; or ``batch_size`` is out of range.
        """
        return await self.delete_in_batches(
            Q(published_at__lt=Timezone.now() - older_than), batch_size, "cleanup_published"
        )

    async def cleanup_dead_lettered(
        self, older_than: timedelta, *, batch_size: int = DEFAULT_CLEANUP_BATCH_SIZE
    ) -> int:
        """Deletes events dead-lettered more than ``older_than`` ago - batch by batch.

        Args:
            older_than: The age of the dead letters deleted.
            batch_size: The events deleted per statement, in ``1..10000``.

        Returns:
            How many events were deleted.

        Raises:
            ConfigurationError: The model has ``Meta.soft_delete_field``, or ``batch_size`` is out
                of range.
        """
        return await self.delete_in_batches(
            Q(dead_lettered_at__lt=Timezone.now() - older_than), batch_size, "cleanup_dead_lettered"
        )

    async def delete_in_batches(self, condition: Q, batch_size: int, method_name: str) -> int:
        """Deletes the relay's events matching a condition, ``batch_size`` per statement.

        Args:
            condition: The condition.
            batch_size: The events per statement.
            method_name: The method deleting, for the message.

        Returns:
            How many events were deleted.

        Raises:
            ConfigurationError: The model has ``Meta.soft_delete_field``, or ``batch_size`` is out
                of range.
        """
        if self.model._meta.soft_delete_field:
            raise ConfigurationError(
                f"{self.model.__name__} has Meta.soft_delete_field set - {method_name}() needs a real DELETE to "
                "free the rows, which QuerySet.delete() would turn into a soft delete"
            )
        self.check_count("batch_size", batch_size, MAX_BATCH_SIZE)
        deleted = 0
        while True:
            sequences = await (
                self.get_topic_queryset()
                .filter(condition)
                .order_by("sequence")
                .limit(batch_size)
                .values_list("sequence", flat=True)
            )
            if not sequences:
                return deleted
            deleted += await self.get_base_queryset().filter(sequence__in=list(sequences)).delete()
            if len(sequences) < batch_size:
                return deleted
