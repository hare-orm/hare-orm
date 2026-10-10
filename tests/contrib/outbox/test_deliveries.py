"""Deliveries that need no broker: TopicRouter routes by topic and pattern, fails a topic without a
route; WebhookDelivery posts the payload, signed, to an HTTP server of the test; the relay batches
through them."""

import asyncio
import hashlib
import hmac
import json

import pytest

from hare.contrib.outbox import DeliveryError, OutboxRelay, TopicRouter, WebhookDelivery
from hare.contrib.outbox.deliveries.constants import EVENT_ID_HEADER, EVENT_TOPIC_HEADER, WEBHOOK_SIGNATURE_HEADER
from hare.exceptions import ConfigurationError
from tests.contrib.outbox.models import DemoOutboxEvent


class HttpReceiver:
    """A minimal HTTP server recording each request and answering with ``status``."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[tuple[dict[str, str], bytes]] = []
        self.server: asyncio.Server | None = None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        headers = {}
        for line in head.decode().split("\r\n")[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        body = await reader.readexactly(int(headers.get("content-length", "0")))
        self.requests.append((headers, body))
        writer.write(f"HTTP/1.1 {self.status} X\r\ncontent-length: 0\r\nconnection: close\r\n\r\n".encode())
        await writer.drain()
        writer.close()

    async def __aenter__(self) -> str:
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/events"

    async def __aexit__(self, *exc_info: object) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()


@pytest.mark.asyncio
async def test_topic_router_routes_by_topic_then_pattern_then_default(db_outbox):
    routed: list[tuple[str, str]] = []

    def route(name: str):
        async def deliver(event: DemoOutboxEvent) -> None:
            routed.append((name, event.topic))

        return deliver

    router = TopicRouter(
        {"shop.order.paid": route("exact"), "shop.order.*": route("pattern")}, default=route("default")
    )
    for topic in ("shop.order.paid", "shop.order.created", "billing.charged"):
        await DemoOutboxEvent.enqueue(topic, {})

    assert await OutboxRelay(DemoOutboxEvent, router, batch_size=10).poll_once() == 3
    assert sorted(routed) == [
        ("default", "billing.charged"),
        ("exact", "shop.order.paid"),
        ("pattern", "shop.order.created"),
    ]


@pytest.mark.asyncio
async def test_a_topic_without_a_route_fails(db_outbox):
    async def deliver(event: DemoOutboxEvent) -> None:
        pass

    event = await DemoOutboxEvent.enqueue("billing.charged", {})
    await OutboxRelay(DemoOutboxEvent, TopicRouter({"shop.*": deliver})).poll_once()

    await event.refresh_from_db()
    assert event.published_at is None
    assert "no delivery takes topic 'billing.charged'" in event.last_error
    with pytest.raises(DeliveryError):
        TopicRouter({}).get_delivery("anything")
    with pytest.raises(ConfigurationError):
        TopicRouter({"": deliver})


@pytest.mark.asyncio
async def test_a_webhook_posts_the_payload_signed(db_outbox):
    event = await DemoOutboxEvent.enqueue("widget.created", {"n": 1}, headers={"author": "alice", "attempt": 2})
    receiver = HttpReceiver()
    async with receiver as url:
        delivery = WebhookDelivery(url, secret="s3cret", headers={"x-service": "shop"})
        relay = OutboxRelay(DemoOutboxEvent, delivery)
        assert await relay.poll_once() == 1
        await delivery.close()

    ((headers, body),) = receiver.requests
    assert json.loads(body) == {"n": 1}
    assert headers[EVENT_ID_HEADER] == str(event.id)
    assert headers[EVENT_TOPIC_HEADER] == "widget.created"
    assert headers["author"] == "alice"
    assert headers["attempt"] == "2"
    assert headers["x-service"] == "shop"
    expected = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert headers[WEBHOOK_SIGNATURE_HEADER] == expected
    await event.refresh_from_db()
    assert event.published_at is not None


@pytest.mark.asyncio
async def test_a_webhook_answering_an_error_fails_the_event(db_outbox):
    event = await DemoOutboxEvent.enqueue("widget.created", {})
    async with HttpReceiver(status=503) as url:
        delivery = WebhookDelivery(url)
        await OutboxRelay(DemoOutboxEvent, delivery).poll_once()
        await delivery.close()

    await event.refresh_from_db()
    assert event.published_at is None
    assert "503" in event.last_error


def test_webhook_options_are_checked():
    with pytest.raises(ConfigurationError, match="url"):
        WebhookDelivery("")
    with pytest.raises(ConfigurationError, match="secret"):
        WebhookDelivery("http://x", secret="")
    with pytest.raises(ConfigurationError, match="timeout_seconds"):
        WebhookDelivery("http://x", timeout_seconds=0)
