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

#: The longest response body - and error text - a webhook delivery writes to its result field.
WEBHOOK_RESULT_BODY_MAX_LENGTH = 4000
#: What a NUL character of a response body becomes in the result field - a database's JSON may hold
#: none, as a body's bytes no text holds become U+FFFD.
WEBHOOK_RESULT_NUL_REPLACEMENT = "\ufffd"

#: The error a webhook delivery writes when allows_url refuses the event's URL.
WEBHOOK_URL_REFUSED_ERROR = "the webhook URL is refused"
