from __future__ import annotations

import itertools
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, TypeVar

from hare.contrib.outbox.outbox_idempotency import OutboxIdempotency
from hare.core.connections.connections import Connections
from hare.core.connections.model_connection_choice import ModelConnectionChoice
from hare.core.hare_context import HareContext
from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.client.transaction_client import TransactionClient
from hare.exceptions import ConfigurationError
from hare.models.instances.instance_initialization import InstanceInitialization
from hare.models.tenancy.tenancy import Tenancy

if TYPE_CHECKING:
    from hare.contrib.outbox.outbox_event import OutboxEvent
    from hare.contrib.outbox.outbox_message import OutboxMessage
    from hare.contrib.outbox.wakeups.outbox_wakeup import OutboxWakeup
    from hare.models.model_binding import ModelBinding


EventType = TypeVar("EventType", bound="OutboxEvent")


class OutboxEnqueue:
    """Coordinates transactional insertion of outbox events and their post-commit notification."""

    @staticmethod
    async def enqueue(
        binding: ModelBinding,
        topic: str,
        payload: Any,
        *,
        using: str | DatabaseClient | None = None,
        idempotency_key: str | None = None,
        ordering_key: str | None = None,
        headers: Mapping[str, Any] | None = None,
        extra_field_values: Mapping[str, Any] | None = None,
        wakeup: OutboxWakeup | None = None,
    ) -> EventType:
        """Writes an outbox event - atomic with the caller's own write when called inside
        ``Transactions.atomic()`` on the same connection. Once the transaction commits (at once
        outside a transaction) the wakeup signals the relays.

        Args:
            binding: The binding of the outbox event model.
            topic: The event's topic - a delivery routes on it.
            payload: The event's payload - JSON values, or values ``OutboxJsonEncoding`` writes.
            using: The connection to write on instead of the model's.
            idempotency_key: At most one stored event exists per key: while it is retained, it is
                returned unchanged instead of writing a new one - no error, no duplicate. Written as
                ``INSERT ... ON CONFLICT (idempotency_key) DO NOTHING`` and a ``SELECT``, so a key
                conflict never aborts an enclosing transaction. After cleanup removes the event, the
                same key may create a new one; retention determines the deduplication window.
            ordering_key: Events of one key are delivered strictly in the order they were written.
            headers: Metadata of the event besides its payload.
            extra_field_values: Values of the columns a subclass declares on top of
                ``OutboxEvent``'s own.
            wakeup: The wakeup to signal instead of the model's ``Meta.outbox_wakeup``.

        Returns:
            The event - the one already stored under ``idempotency_key`` when there is one.

        Raises:
            ValidationError: ``topic``, ``idempotency_key`` or ``ordering_key`` isn't text of 1 to
                255 characters, ``headers`` isn't a mapping of text keys, or ``extra_field_values``
                sets a field ``enqueue()`` or ``OutboxRelay`` sets itself.
            ValidationError: The payload or the headers hold a value without a JSON form here
                (``OutboxJsonEncoding``).
            IntegrityError: ``idempotency_key`` conflicts with an event this call can't see - another
                tenant's, or one committed after this transaction's snapshot; or the event violates
                another unique constraint.
            ConfigurationError: ``using`` wasn't given, the model's connection has no open
                transaction and another connection has one - the event would commit on its own
                instead of with that transaction's write. Pass ``using=`` if this is intended. Or the
                wakeup can't signal on the connection's database (``ListenNotifyWakeup`` without
                listen/notify) - refused before the event is written.
        """
        model: type[EventType] = binding._model  # type: ignore[assignment]
        model.validate_enqueue_options(topic, idempotency_key, ordering_key, headers, extra_field_values)
        field_values = dict(extra_field_values or {})
        write_connection = OutboxEnqueue.get_write_connection(binding, using)
        model.raise_if_wakeup_unsupported(write_connection, wakeup)
        if idempotency_key is None:
            event = (
                await binding.manager.get_bound_queryset(binding, HareContext.require_current())
                .using(write_connection)
                .create(
                    topic=topic,
                    payload=payload,
                    ordering_key=ordering_key,
                    headers=dict(headers or {}),
                    **field_values,
                )
            )
            is_new = True
        else:
            event, is_new = await OutboxIdempotency.insert_or_get_by_idempotency_key(
                binding,
                topic,
                payload,
                idempotency_key,
                ordering_key,
                dict(headers or {}),
                write_connection,
                field_values,
            )
        if is_new:
            await model.signal_wakeup(write_connection, (topic,), wakeup)
        return event

    @staticmethod
    async def enqueue_many(
        binding: ModelBinding,
        messages: Iterable[OutboxMessage],
        *,
        using: str | DatabaseClient | None = None,
        wakeup: OutboxWakeup | None = None,
    ) -> list[EventType]:
        """Writes several outbox events as ``enqueue()`` writes one, in one transaction - an ``INSERT`` per
        run of consecutive events alike (with an idempotency key or without, of one tenant named), the
        events in the order given, so the ones of an ordering key are delivered in that order; one
        signal of the wakeup for all of them.

        Args:
            binding: The binding of the outbox event model.
            messages: The events - ``OutboxMessage(topic, payload, idempotency_key=..., ...)``.
            using: The connection to write on instead of the model's.
            wakeup: The wakeup to signal instead of the model's ``Meta.outbox_wakeup``.

        Returns:
            The events in the order given - for an idempotency key already stored (or given twice), the
            event stored under it.

        Raises:
            ValidationError: A message's arguments, as ``enqueue()`` refuses them.
            IntegrityError: An idempotency key conflicts with an event this call can't see.
            ConfigurationError: As ``enqueue()``.
        """
        # Deferred import: the transactions package imports the models.
        model: type[EventType] = binding._model  # type: ignore[assignment]
        from hare.transactions.atomic.atomic import Atomic
        from hare.transactions.transaction_options import TransactionOptions

        messages = list(messages)
        if not messages:
            return []
        for message in messages:
            model.validate_enqueue_options(
                message.topic,
                message.idempotency_key,
                message.ordering_key,
                message.headers,
                message.extra_field_values,
            )
        write_connection = OutboxEnqueue.get_write_connection(binding, using, "enqueue_many")
        model.raise_if_wakeup_unsupported(write_connection, wakeup)
        events: list[EventType] = []
        inserted_topics: set[str] = set()
        run_events: list[EventType]
        async with Atomic(lambda: write_connection, TransactionOptions.DEFAULT) as transaction_connection:
            for (has_idempotency_key, _), run in itertools.groupby(messages, key=model.get_enqueue_run_key):
                run_messages = list(run)
                run_events, run_topics = await OutboxEnqueue.write_enqueue_run(
                    binding, run_messages, has_idempotency_key, transaction_connection
                )
                events.extend(run_events)
                inserted_topics.update(run_topics)
            if inserted_topics:
                await model.signal_wakeup(transaction_connection, inserted_topics, wakeup)
        return events

    @staticmethod
    def get_enqueue_run_key(binding: ModelBinding, message: OutboxMessage) -> tuple[bool, Any]:
        """What the events of one ``INSERT`` of ``enqueue_many()`` share - an idempotency key or none, and
        the tenant a message names where no tenant scope is active.

        Args:
            binding: The binding of the outbox event model.
            message: The message.

        Returns:
            The key.
        """
        tenant: Any = None
        if binding.tenant_field and Tenancy.get_scope(binding) is None:
            tenant_given, given_tenant, _ = Tenancy.get_given_tenant(binding, message.extra_field_values or {})
            tenant = given_tenant if tenant_given else None
        return message.idempotency_key is not None, tenant

    @staticmethod
    async def write_enqueue_run(
        binding: ModelBinding,
        messages: Sequence[OutboxMessage],
        has_idempotency_key: bool,
        connection: DatabaseClient,
    ) -> tuple[list[EventType], set[str]]:
        """Writes one run of ``enqueue_many()`` - one ``INSERT``, under the tenant its messages name when
        no tenant scope is active.

        Args:
            binding: The binding of the outbox event model.
            messages: The messages of the run.
            has_idempotency_key: Whether they have idempotency keys.
            connection: The connection, in the call's transaction.

        Returns:
            The events of the messages, in their order, and the topics of the ones inserted.
        """
        _, tenant = OutboxEnqueue.get_enqueue_run_key(binding, messages[0])
        if tenant is not None:
            with Tenancy.scope_if_unset(binding, tenant):
                return await OutboxEnqueue.write_enqueue_run_in_scope(
                    binding, messages, has_idempotency_key, connection
                )
        return await OutboxEnqueue.write_enqueue_run_in_scope(binding, messages, has_idempotency_key, connection)

    @staticmethod
    async def write_enqueue_run_in_scope(
        binding: ModelBinding,
        messages: Sequence[OutboxMessage],
        has_idempotency_key: bool,
        connection: DatabaseClient,
    ) -> tuple[list[EventType], set[str]]:
        """Writes one run of ``enqueue_many()`` in the tenant scope it runs under.

        Args:
            binding: The binding of the outbox event model.
            messages: The messages of the run.
            has_idempotency_key: Whether they have idempotency keys.
            connection: The connection, in the call's transaction.

        Returns:
            The events of the messages, in their order, and the topics of the ones inserted.

        Raises:
            IntegrityError: An idempotency key is taken by an event not visible to this call.
        """
        candidates: list[EventType] = [
            InstanceInitialization.new(
                binding,
                HareContext.require_current(),
                topic=message.topic,
                payload=message.payload,
                idempotency_key=message.idempotency_key,
                ordering_key=message.ordering_key,
                headers=dict(message.headers or {}),
                **dict(message.extra_field_values or {}),
            )  # type: ignore[misc]
            for message in messages
        ]
        if not has_idempotency_key:
            topics = {candidate.topic for candidate in candidates}
            if connection.features.supports_returning and connection.features.guarantees_returning_order:
                await (
                    binding.manager.get_bound_queryset(binding, HareContext.require_current())
                    .using(connection)
                    .bulk_create(candidates, returning=True)
                )
                return candidates, topics
            await (
                binding.manager.get_bound_queryset(binding, HareContext.require_current())
                .using(connection)
                .bulk_create(candidates)
            )
            # RETURNING's rows come in no order here - the events are read back by the ids they were given.
            stored_by_id = {
                stored.id: stored
                for stored in await binding.manager.get_bound_queryset(binding, HareContext.require_current())
                .using(connection)
                .filter(id__in=[event.id for event in candidates])
            }
            return [stored_by_id[candidate.id] for candidate in candidates], topics
        conflict_field_names = OutboxIdempotency.get_idempotency_conflict_field_names(
            binding,
        )
        OutboxIdempotency.prepare_candidates(binding, candidates, "enqueue_many")
        inserted = (
            await binding.manager.get_bound_queryset(binding, HareContext.require_current())
            .using(connection)
            .bulk_create(candidates, ignore_conflicts=True, on_conflict=conflict_field_names)
        )
        events = await OutboxIdempotency.read_events_for_inputs(
            binding, candidates, inserted, conflict_field_names, connection
        )
        return events, {event.topic for event in inserted}

    @staticmethod
    def get_write_connection(
        binding: ModelBinding, using: str | DatabaseClient | None, method_name: str = "enqueue"
    ) -> DatabaseClient:
        """The connection an event is written on.

        Args:
            binding: The binding of the outbox model that receives the new event.
            using: The connection asked for, None for the model's.
            method_name: The method writing - named by the error.

        Returns:
            The connection.

        Raises:
            ConfigurationError: ``using`` wasn't given, the model's connection has no open
                transaction and another connection has one.
        """
        model = binding._model
        if using is not None:
            return Connections.get_client(using)  # type: ignore[return-value]
        write_connection = ModelConnectionChoice.get(HareContext.require_current(), binding, True)
        if not write_connection.is_transaction_client:
            for connection_alias in Connections.aliases():
                if connection_alias != write_connection.connection_alias and isinstance(
                    Connections.get(connection_alias), TransactionClient
                ):
                    raise ConfigurationError(
                        f"{model.__name__}.{method_name}() would write on connection "
                        f"{write_connection.connection_alias!r}, "
                        "which has no open transaction, "
                        f"while connection {connection_alias!r} has one - the event would commit on its own, "
                        "not with that transaction's write. Pass using= naming that transaction's connection, "
                        f"or put {model.__name__} on the connection of the models it reports on."
                    )
        return write_connection
