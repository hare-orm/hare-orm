from __future__ import annotations

#: How many consecutive failures a wakeup's subscription tolerates (the first connect or a
#: reconnect) before giving up - the relay then keeps polling.
WAKEUP_MAX_RECONNECT_ATTEMPTS = 5

#: How long a wakeup subscription runs before a failure of it counts from one again, in seconds.
WAKEUP_HEALTHY_SECONDS = 60.0

#: The base of the backoff between a wakeup subscription's reconnects, in seconds.
WAKEUP_RECONNECT_BACKOFF_SECONDS = 0.5

#: The channel, Redis channel, Kafka topic or RabbitMQ exchange a wakeup signals on by default.
DEFAULT_WAKEUP_CHANNEL = "hare_outbox"

#: The separator of the topics one wakeup signal carries.
WAKEUP_TOPIC_SEPARATOR = "\n"

#: How long a Kafka wakeup topic keeps its signals - a relay reads only new ones.
KAFKA_WAKEUP_RETENTION_MILLISECONDS = 60000
