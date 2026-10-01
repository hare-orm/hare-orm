from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self, cast
from uuid import uuid4

from hare import fields
from hare.contrib.outbox.constants import (
    IDEMPOTENCY_KEY_MAX_LENGTH,
    PUBLISH_ARGUMENT_FIELD_NAMES,
    RELAY_MANAGED_FIELD_NAMES,
)
from hare.core.connections import Connections
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.ddl.indexes.partial_index import PartialIndex
from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.client.transaction_client import TransactionClient
from hare.exceptions import ConfigurationError, IntegrityError, QueryError, ValidationError
from hare.models import Model
from hare.models.tenancy import Tenancy
from hare.query.expressions import Q


class OutboxEvent(Model):
    """
    Transactional outbox row: append-only, written by the caller's own business transaction via
    ``publish()`` and later picked up by an ``OutboxRelay`` for delivery.

    Combine with your own project's base model through multiple inheritance:

    Example:
        class MyOutboxEvent(YourBaseModel, OutboxEvent):
            class Meta(YourBaseModel.Meta, OutboxEvent.Meta):
                pass

    ``OutboxEvent.Meta`` declares only ``abstract = True`` and the relay's own ``indexes`` on
    purpose - ``ModelMeta`` merges every abstract ancestor's ``Meta`` attrs via a reversed-MRO
    walk, and silently lets whichever ancestor comes later in that walk win on a key collision.
    Adding anything else to this Meta (a ``table`` name, ...) would risk colliding with
    ``YourBaseModel.Meta``.
    """

    id = fields.UUIDField(primary_key=True, default=uuid4)
    topic = fields.CharField(max_length=255, db_index=True)
    payload = fields.JSONField[dict[str, Any]]()
    created_at = fields.DatetimeField(auto_now_add=True, db_index=True)
    # No db_index: the partial index below covers the relay's query, and a plain one would get the
    # same name.
    published_at = fields.DatetimeField(null=True)
    attempts = fields.IntField(default=0)
    last_error = fields.TextField(null=True)
    # Set only by publish(idempotency_key=...). Unique, but NULLs never conflict with each
    # other (Postgres and SQLite alike), so rows published without a key are unaffected.
    idempotency_key = fields.CharField(max_length=IDEMPOTENCY_KEY_MAX_LENGTH, null=True, unique=True)

    class Meta:
        abstract = True
        indexes = (
            Index(fields=("topic", "created_at")),
            PartialIndex(fields=("published_at",), condition=Q(published_at__isnull=True)),
        )

    @classmethod
    async def publish(
        cls,
        topic: str,
        payload: dict[str, Any],
        *,
        using: str | DatabaseClient | None = None,
        idempotency_key: str | None = None,
        notify_channel: str | None = None,
        extra_field_values: Mapping[str, Any] | None = None,
    ) -> Self:
        """
        Writes an outbox row - atomic with the caller's own business write when called inside
        ``Transactions.atomic()``, since both then resolve to the same connection with
        no extra plumbing needed.

        Args:
            topic: Event topic/routing key an ``OutboxRelay``'s ``deliver`` callable dispatches on.
            payload: JSON-serializable event payload.
            using: Specific DB connection to use instead of the default bound one.
            idempotency_key: If set, at most one row ever exists per key: when a row with this
                key already exists, it is returned unchanged (its own topic/payload) instead of
                writing a new one - no error, no duplicate. Implemented as
                ``INSERT ... ON CONFLICT (idempotency_key) DO NOTHING`` followed by a ``SELECT``,
                so a key conflict never aborts an enclosing Postgres transaction.
            notify_channel: If set and a new row was written, also sends ``NOTIFY`` on this
                channel (payload: the row's id) through the same connection - so inside a
                transaction it is delivered only on commit. Silently ignored on a backend
                without LISTEN/NOTIFY support (SQLite), where relays poll anyway.
            extra_field_values: Values of the columns a subclass declares on top of
                ``OutboxEvent``'s own (e.g. a relation to what the event is about), and optionally
                an explicit primary key.

        Raises:
            ValidationError: ``idempotency_key`` is empty or longer than
                ``IDEMPOTENCY_KEY_MAX_LENGTH``, ``notify_channel`` is empty, or
                ``extra_field_values`` sets ``topic``/``payload``/``idempotency_key`` or a field the
                ORM/relay maintains (``created_at``/``published_at``/``attempts``/``last_error``).
            IntegrityError: ``idempotency_key`` conflicts with a row this call cannot see - one
                belonging to another tenant, or (under REPEATABLE READ/SERIALIZABLE) committed
                after this transaction's snapshot; or the new row violates any other unique
                constraint (primary key, a unique subclass column), keyed or not.
            ConfigurationError: ``using`` wasn't given, this call would land on a connection
                with no active transaction, and a DIFFERENT connection currently has one open -
                the most likely explanation is this model's own connection (``_meta.default_connection``,
                the ``default_connection`` of its app in the config)
                doesn't match the business write's connection (e.g. a single outbox table shared
                across apps that otherwise each use their own connection), so this row would
                commit standalone, immediately, instead of atomically with that other write -
                defeating the whole point of the transactional outbox pattern with no error at
                all otherwise. Pass ``using=`` pointing at that other connection explicitly if
                this really is intentional.
        """
        cls._validate_publish_options(idempotency_key, notify_channel, extra_field_values)
        field_values = dict(extra_field_values or {})
        if using is None:
            write_db = cls.get_connection(True)
            if not isinstance(write_db, TransactionClient):
                for alias in Connections.aliases():
                    if alias == write_db.connection_name:
                        continue
                    if isinstance(Connections.get(alias), TransactionClient):
                        raise QueryError(
                            f"{cls.__name__}.publish() would write on connection "
                            f"{write_db.connection_name!r}, which has no active transaction, but "
                            f"connection {alias!r} currently does - this event would commit "
                            "standalone, immediately, instead of atomically with whatever write "
                            "that other transaction is for, silently breaking the transactional "
                            "outbox guarantee. Pass using= pointing at that transaction's own "
                            f"connection, or configure {cls.__name__} on the same connection as "
                            "the business models it publishes events for."
                        )
        else:
            write_db = cast("DatabaseClient", Connections.get_client(using))
        if idempotency_key is None:
            event = await cls.objects.using(write_db).create(topic=topic, payload=payload, **field_values)
            is_new = True
        else:
            event, is_new = await cls._insert_or_get_by_idempotency_key(
                topic, payload, idempotency_key, write_db, field_values
            )
        if notify_channel is not None and is_new and write_db.features.supports_listen_notify:
            await write_db.notify(notify_channel, str(event.pk))  # type: ignore[attr-defined]
        return event

    @staticmethod
    def _validate_publish_options(
        idempotency_key: str | None, notify_channel: str | None, extra_field_values: Mapping[str, Any] | None
    ) -> None:
        """Checks ``publish()``'s optional arguments.

        Raises:
            ValidationError: If ``idempotency_key``/``notify_channel`` is not a non-empty string of
                allowed length, or ``extra_field_values`` sets a field ``publish()``, the ORM or
                ``OutboxRelay`` sets itself.
        """
        extra_field_names = set(extra_field_values or ())
        argument_field_names = sorted(PUBLISH_ARGUMENT_FIELD_NAMES & extra_field_names)
        if argument_field_names:
            raise ValidationError(
                f"extra_field_values can't set {', '.join(argument_field_names)} - pass them as publish() arguments"
            )
        relay_managed_field_names = sorted(RELAY_MANAGED_FIELD_NAMES & extra_field_names)
        if relay_managed_field_names:
            raise ValidationError(
                f"extra_field_values can't set {', '.join(relay_managed_field_names)} - the ORM and "
                "OutboxRelay maintain them"
            )
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= IDEMPOTENCY_KEY_MAX_LENGTH
        ):
            raise ValidationError(
                f"idempotency_key must be a string of 1..{IDEMPOTENCY_KEY_MAX_LENGTH} characters, "
                f"got {idempotency_key!r}"
            )
        if notify_channel is not None and (not isinstance(notify_channel, str) or not notify_channel):
            raise ValidationError(f"notify_channel must be a non-empty string, got {notify_channel!r}")

    @classmethod
    async def _insert_or_get_by_idempotency_key(
        cls,
        topic: str,
        payload: dict[str, Any],
        idempotency_key: str,
        write_db: DatabaseClient,
        field_values: Mapping[str, Any],
    ) -> tuple[Self, bool]:
        """Inserts a row with ``idempotency_key`` unless one already exists, then reads it back.

        Args:
            topic: Topic for a newly inserted row.
            payload: Payload for a newly inserted row.
            idempotency_key: The unique key.
            write_db: Connection (possibly a transaction client) to run both statements on.
            field_values: Subclass column values for a newly inserted row.

        Returns:
            The row stored under the key, and whether this call inserted it.

        Raises:
            IntegrityError: The key is taken by a row not visible to this call.
            ConfigurationError: ``Meta.tenant_field`` is set, ``field_values`` names no tenant and
                no tenant scope is active; or the named tenant conflicts with the active scope.
        """
        if cls._meta.tenant_field and Tenancy.get_scope(cls) is None:
            tenant_given, given_tenant, _ = Tenancy.get_given_tenant(cls, dict(field_values))
            if tenant_given and given_tenant is not None:
                # An explicitly named tenant is trusted with no active scope, as create() does.
                with Tenancy.scope(given_tenant):
                    return await cls._insert_or_get_by_idempotency_key(
                        topic, payload, idempotency_key, write_db, field_values
                    )
        candidate = cls(topic=topic, payload=payload, idempotency_key=idempotency_key, **field_values)
        # RETURNING tells exactly whether the row was written - where bulk_create() can return rows
        # at all, which needs the database to return them in the order they were written.
        read_back_inserted_row = write_db.dialect.guarantees_returning_order
        conflict_field_names = cls._get_idempotency_conflict_field_names()
        await cls.objects.using(write_db).bulk_create(
            [candidate],
            ignore_conflicts=True,
            on_conflict=conflict_field_names,
            returning=True if read_back_inserted_row else None,
        )
        # The row stored under the key is the one the insert conflicted with - the same values of
        # every field the key is unique with (a tenant, under a scope showing several).
        stored_row_filter = {field_name: getattr(candidate, field_name) for field_name in conflict_field_names}
        queryset = cls.objects.filter(**stored_row_filter).using(write_db)
        if cls._meta.soft_delete_field:
            queryset = queryset.include_deleted()
        stored = await queryset.first()
        if stored is None:
            raise IntegrityError(
                f"{cls.__name__}.publish(): idempotency_key {idempotency_key!r} is already used by a row "
                "not visible here (another tenant's, or committed after this transaction's snapshot)"
            )
        if read_back_inserted_row:
            is_new = candidate._saved_in_db
        else:
            # An explicit primary key alone can't tell this insert apart from an earlier row stored
            # under the same key and id - the created_at this insert wrote can.
            is_new = stored.pk == candidate.pk and stored.created_at == candidate.created_at
        return stored, is_new

    @classmethod
    def _get_idempotency_conflict_field_names(cls) -> list[str]:
        """Returns the unique field set ``idempotency_key`` conflicts on.

        Returns:
            ``["idempotency_key"]`` for a unique column, otherwise the fields of the first
            unconditional ``UniqueConstraint`` that includes it.

        Raises:
            ConfigurationError: No unique constraint covers ``idempotency_key``.
        """
        if cls._meta.fields_map["idempotency_key"].unique:
            return ["idempotency_key"]
        unique_field_sets = [
            *(
                constraint.fields
                for constraint in cls._meta.constraints
                if isinstance(constraint, UniqueConstraint) and constraint.condition is None
            ),
        ]
        for unique_field_names in unique_field_sets:
            if "idempotency_key" in unique_field_names:
                return list(unique_field_names)
        raise ConfigurationError(
            f"{cls.__name__}.idempotency_key isn't unique - publish(idempotency_key=...) needs a unique "
            "constraint covering it to deduplicate on"
        )

    def __repr__(self) -> str:
        return f"<{type(self).__name__} id={self.id} topic={self.topic!r} published_at={self.published_at!r}>"
