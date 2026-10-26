"""A Kafka client of a delivery or a wakeup that fails to start - the broker can't be reached - is
stopped before the error goes on: no client is left open per failed attempt."""

import types

import pytest

from hare.contrib.outbox import KafkaDelivery, KafkaWakeup


class FailingKafkaClient:
    """A producer, consumer or admin client whose start fails - recording its stop."""

    made: list["FailingKafkaClient"] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.stopped = False
        FailingKafkaClient.made.append(self)

    async def start(self) -> None:
        raise ConnectionError("Unable to bootstrap from localhost:1")

    async def stop(self) -> None:
        self.stopped = True

    async def close(self) -> None:
        self.stopped = True


FAILING_AIOKAFKA = types.SimpleNamespace(
    AIOKafkaProducer=FailingKafkaClient,
    AIOKafkaConsumer=FailingKafkaClient,
    admin=types.SimpleNamespace(AIOKafkaAdminClient=FailingKafkaClient, NewTopic=dict),
    errors=types.SimpleNamespace(TopicAlreadyExistsError=type("TopicAlreadyExistsError", (Exception,), {})),
)


@pytest.fixture
def failing_aiokafka(monkeypatch):
    FailingKafkaClient.made.clear()
    monkeypatch.setattr(KafkaDelivery, "aiokafka", FAILING_AIOKAFKA)
    monkeypatch.setattr(KafkaWakeup, "aiokafka", FAILING_AIOKAFKA)
    return FailingKafkaClient.made


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_a_producer_that_fails_to_start_is_stopped_each_attempt(failing_aiokafka):
    delivery = KafkaDelivery("localhost:1", topic="events")
    wakeup = KafkaWakeup("localhost:1")
    for _attempt in range(2):
        with pytest.raises(ConnectionError):
            await delivery.get_producer()
        with pytest.raises(ConnectionError):
            await wakeup.signal(frozenset({"events"}))
    assert len(failing_aiokafka) == 4
    assert all(client.stopped for client in failing_aiokafka)
    assert delivery.producer is None
    assert wakeup.producer is None


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_a_wakeup_subscription_that_fails_to_start_closes_its_clients(failing_aiokafka):
    with pytest.raises(ConnectionError):
        await KafkaWakeup("localhost:1").listen("default")
    with pytest.raises(ConnectionError):
        await KafkaWakeup("localhost:1", create_topic=False).listen("default")
    # The admin client creating the topic, then the consumer.
    assert len(failing_aiokafka) == 2
    assert all(client.stopped for client in failing_aiokafka)
