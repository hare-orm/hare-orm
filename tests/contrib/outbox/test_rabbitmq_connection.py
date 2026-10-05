"""The connection of a RabbitMQ delivery or wakeup: a connection whose exchange can't be opened is closed,
never left reconnecting behind the next one."""

import pytest

from hare.contrib.outbox.brokers.rabbitmq_connection import RabbitMQConnection


class RecordedConnection:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class RecordingAioPika:
    """Answers ``connect_robust()`` as ``aio_pika`` does, keeping every connection opened."""

    def __init__(self) -> None:
        self.connections: list[RecordedConnection] = []

    async def connect_robust(self, url: str) -> RecordedConnection:
        connection = RecordedConnection()
        self.connections.append(connection)
        return connection


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_a_connection_whose_exchange_isnt_opened_is_closed():
    opened_exchanges = 0

    async def open_exchange(connection: RecordedConnection) -> object:
        nonlocal opened_exchanges
        opened_exchanges += 1
        if opened_exchanges == 1:
            raise PermissionError("ACCESS_REFUSED")
        return "exchange"

    rabbitmq_connection = RabbitMQConnection("amqp://broker", open_exchange)
    aio_pika = rabbitmq_connection.aio_pika = RecordingAioPika()
    with pytest.raises(PermissionError):
        await rabbitmq_connection.get_exchange()
    assert await rabbitmq_connection.get_exchange() == "exchange"
    assert [connection.closed for connection in aio_pika.connections] == [True, False]

    await rabbitmq_connection.close()
    assert [connection.closed for connection in aio_pika.connections] == [True, True]
