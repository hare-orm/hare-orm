"""Where the outbox relays send events - Kafka, RabbitMQ, Redis streams, an HTTP endpoint, or a
delivery per topic (``TopicRouter``)."""

from __future__ import annotations

from hare.contrib.outbox.deliveries.callable_delivery import CallableDelivery
from hare.contrib.outbox.deliveries.kafka_delivery import KafkaDelivery
from hare.contrib.outbox.deliveries.outbox_delivery import OutboxDelivery
from hare.contrib.outbox.deliveries.rabbitmq_delivery import RabbitMQDelivery
from hare.contrib.outbox.deliveries.redis_streams_delivery import RedisStreamsDelivery
from hare.contrib.outbox.deliveries.topic_router import TopicRouter
from hare.contrib.outbox.deliveries.webhook_delivery import WebhookDelivery

__all__ = [
    "CallableDelivery",
    "KafkaDelivery",
    "OutboxDelivery",
    "RabbitMQDelivery",
    "RedisStreamsDelivery",
    "TopicRouter",
    "WebhookDelivery",
]
