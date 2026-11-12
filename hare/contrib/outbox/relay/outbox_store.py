from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from operator import attrgetter
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from hare.classes.value_descriptions import ValueDescriptions
from hare.contrib.outbox.constants import ENQUEUE_ARGUMENT_FIELD_NAMES, RELAY_MANAGED_FIELD_NAMES
from hare.contrib.outbox.deliveries.delivery_batch import DeliveryBatch
from hare.contrib.outbox.deliveries.messages import INVALID_RESULT_FIELDS
from hare.contrib.outbox.outbox_event import OutboxEvent
from hare.contrib.outbox.relay.claimed_batch import ClaimedBatch
from hare.contrib.outbox.relay.constants import DEFAULT_CLEANUP_BATCH_SIZE
from hare.contrib.outbox.relay.outbox_backlog import OutboxBacklog
from hare.contrib.outbox.relay.topic_selection import TopicSelection
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.query.constants import PRIMARY_KEY_ALIAS
from hare.query.expressions import Exists, OuterReference, Q
from hare.time import Timezone

if TYPE_CHECKING:
    from hare.contrib.outbox.deliveries.delivery_result import DeliveryResult
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.query.queryset import QuerySet

TOutboxEvent = TypeVar("TOutboxEvent", bound=OutboxEvent)


class OutboxStore(Generic[TOutboxEvent]):
    """Persists claims and delivery outcomes on one explicit database client.

    Every query, including maintenance and candidate subqueries, uses this client.
    A fresh token identifies each claim. Failures and attempt metadata need that token;
    a late acknowledgement may still publish an unpublished event.
    """

    def __init__(
        self,
        model: type[TOutboxEvent],
        client: DatabaseClient,
        *,
        topics: TopicSelection | None = None,
    ) -> None:
        self.model = model
        self.client = client
        self.topic_selection = topics if topics is not None else TopicSelection(None)

    @staticmethod
    def check_count(option_name: str, value: Any) -> None:
        """Checks a count option.

        Raises:
            ConfigurationError: It isn't a positive int.
        """
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigurationError(
                f"{option_name} must be a positive int, got {ValueDescriptions.get_shown_value(value)}"
            )

    def get_base_queryset(self) -> QuerySet[TOutboxEvent]:
        """The queryset every relay query starts from - every tenant's events: the relay serves the
        whole table, no tenant scope around it."""
        if self.model._meta.tenant_field:
            return self.model.objects.all_tenants().using(self.client)
        return self.model.objects.all().using(self.client)

    def get_topic_queryset(self) -> QuerySet[TOutboxEvent]:
        """The relay's events - of its ``topics``, none of its ``exclude_topics``."""
        queryset = self.get_base_queryset()
        condition = self.topic_selection.get_condition()
        return queryset if condition is None else queryset.filter(condition)

    async def claim(self, *, name: str, batch_size: int, lease_seconds: float) -> ClaimedBatch[TOutboxEvent]:
        """Leases the next batch of due events to this relay.

        Returns:
            The fresh claim token and events in sequence order.

        Raises:
            UnSupportedError: The database returns no rows of a write - a lease is taken by
                ``UPDATE ... RETURNING``.
        """
        connection = self.client
        if not connection.features.supports_returning:
            raise UnSupportedError(
                f"OutboxRelay leases events by UPDATE ... RETURNING, which the {connection.dialect.name} database "
                "runs none of - relay the events of an outbox on a database whose writes return their rows "
                "(Features.supports_returning)"
            )
        token = uuid.uuid4()
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
            .limit(batch_size)
        )
        if connection.features.supports_select_for_update:
            candidates = candidates.select_for_update(skip_locked=True)
        claimed = await (
            self.get_base_queryset()
            .filter(sequence__in=candidates.values("sequence"))
            .update(lease_until=now + timedelta(seconds=lease_seconds), leased_by=name, lease_token=token)
            .returning()
        )
        return ClaimedBatch(token, tuple(sorted(claimed, key=attrgetter("sequence"))))

    async def renew(self, batch: ClaimedBatch[TOutboxEvent], *, lease_seconds: float) -> None:
        """Extends only rows still held by this claim, without reclaiming handed-off rows."""
        await (
            self.get_base_queryset()
            .filter(
                sequence__in=[event.sequence for event in batch.events],
                lease_token=batch.token,
                published_at__isnull=True,
            )
            .update(lease_until=Timezone.now() + timedelta(seconds=lease_seconds))
        )

    def get_metadata(self, result: DeliveryResult) -> dict[str, Any]:
        """Checks that transport metadata cannot replace queue identity or lease state."""
        metadata = dict(result.metadata)
        primary_key = self.model._meta.primary_key_attribute
        key_fields = {primary_key} if isinstance(primary_key, str) else set(primary_key)
        forbidden = set(metadata) & (
            RELAY_MANAGED_FIELD_NAMES | ENQUEUE_ARGUMENT_FIELD_NAMES | {"id", PRIMARY_KEY_ALIAS} | key_fields
        )
        if forbidden:
            raise ConfigurationError(INVALID_RESULT_FIELDS.format(fields=", ".join(sorted(forbidden))))
        return metadata

    async def acknowledge(
        self,
        batch: ClaimedBatch[TOutboxEvent],
        results: Sequence[DeliveryResult],
        now: datetime,
    ) -> set[uuid.UUID]:
        """Publishes successes, retaining metadata only for the current claim.

        Ordinary acknowledgements share one statement. Metadata and publication are written
        together for a current attempt; a stale attempt can acknowledge without its metadata.
        Already published rows and their metadata remain unchanged.
        """
        DeliveryBatch.check(batch.events, results)
        published: set[uuid.UUID] = set()
        successes = [result for result in results if result.error is None]
        for result in successes:
            metadata = self.get_metadata(result)
            if not metadata:
                continue
            recorded = (
                await self.get_base_queryset()
                .filter(
                    id=result.event_id,
                    lease_token=batch.token,
                    published_at__isnull=True,
                )
                .update(
                    **metadata,
                    published_at=now,
                    lease_until=None,
                    leased_by=None,
                    lease_token=None,
                )
            )
            if recorded:
                published.add(result.event_id)
        remaining = [result.event_id for result in successes if result.event_id not in published]
        if remaining:
            rows = (
                await self.get_base_queryset()
                .filter(
                    id__in=remaining,
                    published_at__isnull=True,
                )
                .update(published_at=now, lease_until=None, leased_by=None, lease_token=None)
                .returning()
            )
            published.update(row.id for row in rows)
        return published

    async def record_failure(
        self,
        batch: ClaimedBatch[TOutboxEvent],
        result: DeliveryResult,
        *,
        attempts: int,
        error_text: str,
        next_attempt_at: datetime | None,
        dead_lettered_at: datetime | None,
    ) -> bool:
        """Records a failure and its metadata only while this claim is current."""
        metadata = self.get_metadata(result)
        recorded = (
            await self.get_base_queryset()
            .filter(
                id=result.event_id,
                lease_token=batch.token,
                published_at__isnull=True,
            )
            .update(
                **metadata,
                attempts=attempts,
                last_error=error_text,
                next_attempt_at=next_attempt_at,
                dead_lettered_at=dead_lettered_at,
                lease_until=None,
                leased_by=None,
                lease_token=None,
            )
        )
        return bool(recorded)

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
            topics: Only events of these topics - names, or prefix patterns ending in ``*``.
            ids: Only these events.

        Returns:
            How many events were given back.

        Raises:
            ConfigurationError: A topic pattern has ``*`` or ``?`` anywhere but a ``*`` at its end.
        """
        queryset = self.get_topic_queryset().filter(dead_lettered_at__isnull=False)
        topic_condition = TopicSelection(topics).get_condition()
        if topic_condition is not None:
            queryset = queryset.filter(topic_condition)
        if ids is not None:
            queryset = queryset.filter(id__in=list(ids))
        # The outbox's database reports the rows a write changed - its unique fields refuse any other.
        count: int = await queryset.update(dead_lettered_at=None, attempts=0, next_attempt_at=None)
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
        return await self.delete_in_batches(
            Q(published_at__lt=Timezone.now() - older_than), batch_size, "cleanup_published"
        )

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
        self.check_count("batch_size", batch_size)
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
            deleted += await self.get_topic_queryset().filter(condition, sequence__in=list(sequences)).delete()
            if len(sequences) < batch_size:
                return deleted
