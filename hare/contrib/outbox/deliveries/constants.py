from __future__ import annotations

#: The header carrying an event's id in a Kafka, RabbitMQ, Redis or webhook delivery.
EVENT_ID_HEADER = "hare-outbox-event-id"

#: The header carrying an event's topic.
EVENT_TOPIC_HEADER = "hare-outbox-topic"

#: The header carrying the HMAC-SHA256 signature of a webhook body.
WEBHOOK_SIGNATURE_HEADER = "hare-outbox-signature"

#: How a webhook signature is written - the hex digest after this prefix.
WEBHOOK_SIGNATURE_PREFIX = "sha256="

#: The longest webhook request, in seconds, by default.
DEFAULT_WEBHOOK_TIMEOUT_SECONDS = 10.0

#: The field of a Redis stream entry holding the event's JSON.
REDIS_STREAM_EVENT_FIELD = "event"
