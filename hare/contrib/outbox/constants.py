DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_BATCH_SIZE = 100
DEFAULT_MAX_DELIVERY_ATTEMPTS = 5
DEFAULT_BACKOFF_BASE_SECONDS = 0.5

#: How many consecutive failures OutboxRelay._run_listen_loop() tolerates (initial connect or a
#: reconnect after the listener died) before giving up on LISTEN/NOTIFY for good and falling back
#: to polling-only for the rest of this relay's lifetime.
LISTEN_MAX_RECONNECT_ATTEMPTS = 5


#: Longest idempotency key OutboxEvent.publish() accepts (the column is a VARCHAR of this size).
IDEMPOTENCY_KEY_MAX_LENGTH = 255

#: Fields OutboxEvent.publish() sets from its own arguments - extra_field_values may not set them.
PUBLISH_ARGUMENT_FIELD_NAMES = frozenset({"topic", "payload", "idempotency_key"})

#: Fields the ORM and OutboxRelay maintain - extra_field_values may not set them either.
RELAY_MANAGED_FIELD_NAMES = frozenset({"created_at", "published_at", "attempts", "last_error"})


#: Largest accepted OutboxRelay(poll_interval_seconds=...) - one day.
MAX_POLL_INTERVAL_SECONDS = 86400.0

#: Largest accepted OutboxRelay(batch_size=...).
MAX_BATCH_SIZE = 10000

#: Largest accepted OutboxRelay(max_delivery_attempts=...).
MAX_DELIVERY_ATTEMPTS_LIMIT = 10000

#: Largest accepted OutboxRelay(backoff_base_seconds=...) - one hour.
MAX_BACKOFF_BASE_SECONDS = 3600.0
