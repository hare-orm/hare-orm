from __future__ import annotations

DEFAULT_POLL_INTERVAL_SECONDS = 5.0

DEFAULT_BATCH_SIZE = 100

DEFAULT_MAX_DELIVERY_ATTEMPTS = 5

DEFAULT_RETRY_BASE_SECONDS = 1.0

DEFAULT_RETRY_MAX_SECONDS = 300.0

#: The random spread of a retry's pause - up to this share of it, either way.
RETRY_JITTER_RATIO = 0.1

DEFAULT_LEASE_SECONDS = 60.0

DEFAULT_DELIVERY_TIMEOUT_SECONDS = 30.0

DEFAULT_CONCURRENCY = 10

DEFAULT_CLEANUP_BATCH_SIZE = 1000

#: Largest accepted ``batch_size`` and cleanup batch size.
MAX_BATCH_SIZE = 10000

#: Largest accepted ``max_delivery_attempts``.
MAX_DELIVERY_ATTEMPTS_LIMIT = 10000

#: Largest accepted ``concurrency``.
MAX_CONCURRENCY = 1000

#: The longest ``last_error`` kept of a failed delivery.
LAST_ERROR_MAX_LENGTH = 4000
