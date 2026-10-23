"""What wakes the outbox relays once events are written - in the same process, through the
database's LISTEN/NOTIFY, Redis, Kafka or RabbitMQ."""

from __future__ import annotations

from hare.contrib.outbox.wakeups.in_process_wakeup import InProcessWakeup
from hare.contrib.outbox.wakeups.kafka_wakeup import KafkaWakeup
from hare.contrib.outbox.wakeups.listen_notify_wakeup import ListenNotifyWakeup
from hare.contrib.outbox.wakeups.outbox_wakeup import OutboxWakeup
from hare.contrib.outbox.wakeups.rabbitmq_wakeup import RabbitMQWakeup
from hare.contrib.outbox.wakeups.redis_wakeup import RedisWakeup

__all__ = [
    "InProcessWakeup",
    "KafkaWakeup",
    "ListenNotifyWakeup",
    "OutboxWakeup",
    "RabbitMQWakeup",
    "RedisWakeup",
]
