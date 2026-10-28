"""WebhookDelivery of subscribers: each event's endpoint, secret and headers from target=, a URL check before
every request (allows_url=), each attempt's result written to the event's row (result_field=), no
redirect followed."""

import hashlib
import hmac

import pytest

from hare.contrib.outbox import OutboxRelay, WebhookDelivery, WebhookTarget
from hare.contrib.outbox.deliveries.constants import (
    WEBHOOK_RESULT_BODY_MAX_LENGTH,
    WEBHOOK_SIGNATURE_HEADER,
    WEBHOOK_URL_REFUSED_ERROR,
)
from hare.exceptions import ConfigurationError
from tests.contrib.outbox.http_receiver import HttpReceiver
from tests.contrib.outbox.models import SubscriberOutboxEvent

pytestmark = pytest.mark.requires("http")


async def enqueue_for(url: str, secret: str | None = None) -> SubscriberOutboxEvent:
    return await SubscriberOutboxEvent.enqueue(
        "widget.created",
        {"n": 1},
        extra_field_values={"subscriber_url": url, "subscriber_secret": secret},
    )


def get_subscriber_target(event: SubscriberOutboxEvent) -> WebhookTarget:
    return WebhookTarget(event.subscriber_url, event.subscriber_secret, {"x-subscriber": event.subscriber_url[-6:]})


async def deliver_all(delivery: WebhookDelivery) -> None:
    await OutboxRelay(SubscriberOutboxEvent, delivery).poll_once()
    await delivery.close()


@pytest.mark.asyncio
async def test_each_event_goes_to_its_target_signed_with_its_secret(db_outbox):
    first_receiver, second_receiver = HttpReceiver(), HttpReceiver()
    async with first_receiver as first_url, second_receiver as second_url:
        await enqueue_for(first_url, "first-secret")
        await enqueue_for(second_url)

        async def get_target(event):
            return get_subscriber_target(event)

        await deliver_all(WebhookDelivery(target=get_target, headers={"x-service": "shop"}))

    ((first_headers, first_body),) = first_receiver.requests
    ((second_headers, __),) = second_receiver.requests
    assert first_headers[WEBHOOK_SIGNATURE_HEADER] == (
        "sha256=" + hmac.new(b"first-secret", first_body, hashlib.sha256).hexdigest()
    )
    assert WEBHOOK_SIGNATURE_HEADER not in second_headers
    assert (first_headers["x-subscriber"], first_headers["x-service"]) == (first_url[-6:], "shop")
    assert await SubscriberOutboxEvent.objects.filter(published_at__isnull=True).count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True])
async def test_a_refused_url_is_not_requested_and_its_event_is_tried_again(db_outbox, is_async):
    receiver = HttpReceiver()
    checked_urls: list[str] = []

    def allows_url(url):
        checked_urls.append(url)
        return False

    async def allows_url_async(url):
        return allows_url(url)

    async with receiver as url:
        event = await enqueue_for(url)
        await deliver_all(
            WebhookDelivery(
                target=get_subscriber_target,
                allows_url=allows_url_async if is_async else allows_url,
                result_field="webhook_result",
            )
        )

    assert receiver.requests == []
    assert checked_urls == [url]
    await event.refresh_from_db()
    assert event.published_at is None
    assert event.attempts == 1
    assert "refused" in event.last_error
    assert event.webhook_result == {"status_code": None, "body": None, "error": WEBHOOK_URL_REFUSED_ERROR}


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "is_published"), [(200, True), (500, False)])
async def test_the_result_of_an_answer_is_written(db_outbox, status, is_published):
    long_body = "x" * (WEBHOOK_RESULT_BODY_MAX_LENGTH + 10)
    async with HttpReceiver(status=status, body=long_body) as url:
        event = await enqueue_for(url)
        await deliver_all(WebhookDelivery(target=get_subscriber_target, result_field="webhook_result"))

    await event.refresh_from_db()
    assert (event.published_at is not None) is is_published
    assert event.webhook_result == {
        "status_code": status,
        "body": long_body[:WEBHOOK_RESULT_BODY_MAX_LENGTH],
        "error": None,
    }


@pytest.mark.asyncio
async def test_a_body_with_a_nul_is_written_and_its_event_delivered_once(db_outbox):
    receiver = HttpReceiver(body="ok\x00")
    async with receiver as url:
        event = await enqueue_for(url)
        await deliver_all(WebhookDelivery(target=get_subscriber_target, result_field="webhook_result"))

    await event.refresh_from_db()
    assert len(receiver.requests) == 1
    assert event.published_at is not None
    assert event.webhook_result == {"status_code": 200, "body": "ok\ufffd", "error": None}


@pytest.mark.asyncio
async def test_the_error_of_a_request_without_an_answer_is_written(db_outbox):
    # A scheme the client has no transport for fails before any connection - a proxy of the machine
    # can't answer it.
    event = await enqueue_for("unsupported://127.0.0.1/events")
    await deliver_all(WebhookDelivery(target=get_subscriber_target, result_field="webhook_result"))

    await event.refresh_from_db()
    assert event.published_at is None
    assert event.webhook_result["status_code"] is None
    assert event.webhook_result["error"]
    assert "request" in event.last_error


@pytest.mark.asyncio
async def test_a_redirect_is_not_followed(db_outbox):
    redirected_receiver = HttpReceiver()
    async with redirected_receiver as redirected_url:
        async with HttpReceiver(status=302, location=redirected_url) as url:
            event = await enqueue_for(url)
            await deliver_all(WebhookDelivery(url, result_field="webhook_result"))

    assert redirected_receiver.requests == []
    await event.refresh_from_db()
    assert event.published_at is None
    assert event.webhook_result["status_code"] == 302


@pytest.mark.database_independent
def test_the_endpoint_is_given_one_way():
    with pytest.raises(ConfigurationError, match="url or target"):
        WebhookDelivery()
    with pytest.raises(ConfigurationError, match="url or target"):
        WebhookDelivery("http://example.com", target=get_subscriber_target)
    with pytest.raises(ConfigurationError, match="WebhookTarget"):
        WebhookDelivery(target=get_subscriber_target, secret="s")


@pytest.mark.asyncio
async def test_the_signature_goes_in_the_header_named(db_outbox):
    receiver = HttpReceiver()
    async with receiver as url:
        await enqueue_for(url, "secret")
        await deliver_all(WebhookDelivery(target=get_subscriber_target, signature_header="X-Webhook-Signature"))

    ((headers, body),) = receiver.requests
    assert headers["x-webhook-signature"] == "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert WEBHOOK_SIGNATURE_HEADER not in headers
