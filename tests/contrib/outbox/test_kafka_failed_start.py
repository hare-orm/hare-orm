"""A Kafka client of a delivery or a wakeup that fails to start - the broker can't be reached - is
stopped before the error goes on: no client is left open per failed attempt."""

import asyncio
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
            await delivery.broker_connection.get_producer()
        with pytest.raises(ConnectionError):
            await wakeup.signal(frozenset({"events"}))
    assert len(failing_aiokafka) == 4
    assert all(client.stopped for client in failing_aiokafka)
    assert delivery.broker_connection.producer is None
    assert wakeup.broker_connection.producer is None


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


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["delivery", "wakeup", "admin", "consumer"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_client_start_and_cleanup_failures_are_both_retained(
    failing_aiokafka, monkeypatch, transport, cancelled
):
    failure = asyncio.CancelledError("startup") if cancelled else ConnectionError("startup")
    release_failure = RuntimeError("cleanup")

    async def start(self):
        raise failure

    async def stop(self):
        self.stopped = True
        raise release_failure

    monkeypatch.setattr(FailingKafkaClient, "start", start)
    monkeypatch.setattr(FailingKafkaClient, "stop", stop)
    monkeypatch.setattr(FailingKafkaClient, "close", stop)
    client = KafkaDelivery("localhost:1") if transport == "delivery" else KafkaWakeup("localhost:1")
    with pytest.raises(BaseExceptionGroup) as captured:
        if transport == "admin":
            await client.ensure_topic()
        elif transport == "consumer":
            client.create_topic = False
            await client.listen("default")
        else:
            await client.broker_connection.get_producer()
    assert captured.value.exceptions == (failure, release_failure)
    assert client.broker_connection.producer is None
    assert failing_aiokafka[0].stopped


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_application_producer_is_neither_started_nor_closed(failing_aiokafka):
    producer = FailingKafkaClient()
    delivery = KafkaDelivery(producer=producer)
    assert await delivery.broker_connection.get_producer() is producer
    await delivery.close()
    assert not producer.stopped
    assert await delivery.broker_connection.get_producer() is producer
