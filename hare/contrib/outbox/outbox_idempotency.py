from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.dialects.base.client.database_client import DatabaseClient
from hare.exceptions import ConfigurationError, IntegrityError
from hare.models.tenancy.tenancy import Tenancy

if TYPE_CHECKING:
    from hare.contrib.outbox.outbox_event import OutboxEvent


EventType = TypeVar("EventType", bound="OutboxEvent")


class OutboxIdempotency:
    """Inserts keyed events atomically and reads the matching event after a key conflict."""

    @staticmethod
    async def insert_or_get_by_idempotency_key(
        model: type[EventType],
        topic: str,
        payload: Any,
        idempotency_key: str,
        ordering_key: str | None,
        headers: dict[str, Any],
        write_connection: DatabaseClient,
        field_values: Mapping[str, Any],
    ) -> tuple[EventType, bool]:
        """Inserts an event with ``idempotency_key`` unless one already exists, then reads it back.

        Args:
            topic: The topic of a new event.
            payload: The payload of a new event.
            idempotency_key: The unique key.
            ordering_key: The ordering key of a new event.
            headers: The headers of a new event.
            write_connection: The connection - possibly a transaction - both statements run on.
            field_values: Subclass column values of a new event.

        Returns:
            The event stored under the key, and whether this call inserted it.

        Raises:
            IntegrityError: The key is taken by an event not visible to this call.
            ConfigurationError: ``Meta.tenant_field`` is set, ``field_values`` names no tenant and
                no tenant scope is active; or the named tenant conflicts with the active scope.
        """
        if model._meta.tenant_field and Tenancy.get_scope(model) is None:
            tenant_given, given_tenant, _ = Tenancy.get_given_tenant(model, dict(field_values))
            if tenant_given and given_tenant is not None:
                # An explicitly named tenant is trusted with no active scope, as create() does.
                with Tenancy.scope(given_tenant):
                    return await model.insert_or_get_by_idempotency_key(
                        topic, payload, idempotency_key, ordering_key, headers, write_connection, field_values
                    )
        candidate = model(
            topic=topic,
            payload=payload,
            idempotency_key=idempotency_key,
            ordering_key=ordering_key,
            headers=headers,
            **field_values,
        )
        # RETURNING tells exactly whether the row was written - where bulk_create() can return rows
        # at all, which needs the database to return them in the order they were written.
        read_back_inserted_row = write_connection.features.guarantees_returning_order
        conflict_field_names = model.get_idempotency_conflict_field_names()
        await model.objects.using(write_connection).bulk_create(
            [candidate],
            ignore_conflicts=True,
            on_conflict=conflict_field_names,
            returning=True if read_back_inserted_row else None,
        )
        # The row stored under the key is the one the insert conflicted with - the same values of
        # every field the key is unique with (a tenant, under a scope showing several).
        stored_row_filter = {field_name: getattr(candidate, field_name) for field_name in conflict_field_names}
        queryset = model.objects.filter(**stored_row_filter).using(write_connection)
        if model._meta.soft_delete_field:
            queryset = queryset.include_deleted()
        stored = await queryset.first()
        if stored is None:
            raise IntegrityError(
                f"{model.__name__}.enqueue(): idempotency_key {idempotency_key!r} is already used by an event "
                "not visible here (another tenant's, or committed after this transaction's snapshot)"
            )
        if read_back_inserted_row:
            is_new = candidate._saved_in_db
        else:
            # An explicit id alone can't tell this insert apart from an earlier event stored under the
            # same key and id - the created_at this insert wrote can.
            is_new = stored.id == candidate.id and stored.created_at == candidate.created_at
        return stored, is_new

    @staticmethod
    def get_idempotency_conflict_field_names(model: type[EventType]) -> list[str]:
        """The unique field set ``idempotency_key`` conflicts on.

        Returns:
            ``["idempotency_key"]`` for a unique column, otherwise the fields of the first
            unconditional ``UniqueConstraint`` that includes it.

        Raises:
            ConfigurationError: No unique constraint covers ``idempotency_key``.
        """
        if model._meta.fields_map["idempotency_key"].unique:
            return ["idempotency_key"]
        for constraint in model._meta.constraints:
            if (
                isinstance(constraint, UniqueConstraint)
                and constraint.condition is None
                and "idempotency_key" in constraint.fields
            ):
                return list(constraint.fields)
        raise ConfigurationError(
            f"{model.__name__}.idempotency_key isn't unique - enqueue(idempotency_key=...) needs a unique "
            "constraint covering it to deduplicate on"
        )
