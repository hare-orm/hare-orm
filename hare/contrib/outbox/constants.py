from __future__ import annotations

#: The longest topic, idempotency key, ordering key and relay name - the columns are VARCHARs of
#: this size.
OUTBOX_KEY_MAX_LENGTH = 255
#: The Meta option of an outbox model naming the wakeup its events signal.
OUTBOX_WAKEUP_META_OPTION = "outbox_wakeup"

#: Fields ``OutboxEvent.enqueue()`` sets from its own arguments - ``extra_field_values`` may not set
#: them.
ENQUEUE_ARGUMENT_FIELD_NAMES = frozenset({"topic", "payload", "idempotency_key", "ordering_key", "headers"})

#: Fields the ORM and ``OutboxRelay`` maintain - ``extra_field_values`` may not set them either.
RELAY_MANAGED_FIELD_NAMES = frozenset(
    {
        "sequence",
        "created_at",
        "next_attempt_at",
        "published_at",
        "dead_lettered_at",
        "attempts",
        "last_error",
        "lease_until",
        "leased_by",
    }
)


#: Largest accepted ``poll_interval_seconds``, ``retry_base_seconds``, ``retry_max_seconds``,
#: ``lease_seconds`` and ``delivery_timeout_seconds`` - one day.
MAX_INTERVAL_SECONDS = 86400.0
