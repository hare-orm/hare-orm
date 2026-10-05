"""The transactional outbox: events written in the transaction of the change they report
(``OutboxEvent.enqueue()``, or a model's ``Meta.change_capture = ChangeCapture(...)``) and delivered
afterwards, at least once, by an ``OutboxRelay`` - through a delivery (Kafka, RabbitMQ, Redis
streams, a webhook, taskiq) and woken by a wakeup."""

from __future__ import annotations

from hare.contrib.outbox.capture import ChangeCapture, ChangeExtension
from hare.contrib.outbox.deliveries import (
    CallableDelivery,
    KafkaDelivery,
    OutboxDelivery,
    RabbitMQDelivery,
    RedisStreamsDelivery,
    TopicRouter,
    WebhookDelivery,
)
from hare.contrib.outbox.exceptions import DeliveryError
from hare.contrib.outbox.outbox_event import OutboxEvent
from hare.contrib.outbox.outbox_json_encoding import OutboxJsonEncoding
from hare.contrib.outbox.relay import (
    OutboxBacklog,
    OutboxDeadLettered,
    OutboxDelivered,
    OutboxDeliveryFailed,
    OutboxRelay,
)
from hare.contrib.outbox.wakeups import (
    InProcessWakeup,
    KafkaWakeup,
    ListenNotifyWakeup,
    OutboxWakeup,
    RabbitMQWakeup,
    RedisWakeup,
)
from hare.instrumentation.capture.captured_change import CapturedChange
from hare.instrumentation.enums import ChangePayload

__all__ = [
    "CallableDelivery",
    "CapturedChange",
    "ChangeCapture",
    "ChangeExtension",
    "ChangePayload",
    "DeliveryError",
    "InProcessWakeup",
    "KafkaDelivery",
    "KafkaWakeup",
    "ListenNotifyWakeup",
    "OutboxBacklog",
    "OutboxDeadLettered",
    "OutboxDelivered",
    "OutboxDeliveryFailed",
    "OutboxDelivery",
    "OutboxEvent",
    "OutboxJsonEncoding",
    "OutboxRelay",
    "OutboxWakeup",
    "RabbitMQDelivery",
    "RabbitMQWakeup",
    "RedisStreamsDelivery",
    "RedisWakeup",
    "TopicRouter",
    "WebhookDelivery",
]
