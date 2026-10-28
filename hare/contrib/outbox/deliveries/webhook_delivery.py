from __future__ import annotations

import hashlib
import hmac
import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

from hare.contrib.outbox.deliveries.constants import (
    DEFAULT_WEBHOOK_TIMEOUT_SECONDS,
    WEBHOOK_RESULT_BODY_MAX_LENGTH,
    WEBHOOK_RESULT_NUL_REPLACEMENT,
    WEBHOOK_SIGNATURE_HEADER,
    WEBHOOK_SIGNATURE_PREFIX,
    WEBHOOK_URL_REFUSED_ERROR,
)
from hare.contrib.outbox.deliveries.outbox_delivery import OutboxDelivery
from hare.contrib.outbox.deliveries.webhook_target import WebhookTarget
from hare.contrib.outbox.exceptions import DeliveryError
from hare.contrib.package_checks import PackageChecks
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.contrib.outbox.outbox_event import OutboxEvent

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]


class WebhookDelivery(OutboxDelivery):
    """Sends events to an HTTP endpoint - a ``POST`` of the payload's JSON with the event's headers,
    signed with HMAC-SHA256 of the body when a secret is given (``signature_header``:
    ``sha256=<hex digest>``). A header value is sent as its UTF-8 bytes - the client writes a text
    value only in ASCII. A response other than 2xx fails the event, and a redirect is not followed.
    Needs the ``http`` extra.

    Args:
        url: The endpoint of every event - or ``target``.
        target: A function of the event - sync or async - giving its ``WebhookTarget``: the endpoint,
            secret and headers of each event's subscriber.
        secret: The key the body of every event of ``url`` is signed with; None sends it unsigned.
        timeout_seconds: The longest a request may take.
        headers: Headers sent with every request.
        allows_url: A function of a URL - sync or async - refusing it with False, checked before every
            request: an event of a refused URL is not sent and fails.
        signature_header: The header carrying the signature - ``WEBHOOK_SIGNATURE_HEADER``
            (``hare-outbox-signature``) by default, another name for receivers expecting their own.
        result_field: A field of the outbox model each attempt's result is written to -
            ``{"status_code", "body", "error"}``, the body and the error cut to
            ``WEBHOOK_RESULT_BODY_MAX_LENGTH`` characters; None writes none.

    Raises:
        ConfigurationError: The ``httpx`` package isn't installed, or neither or both of ``url`` and
            ``target`` are given, or ``secret`` with ``target``.
    """

    #: The ``httpx`` package, None without the ``http`` extra.
    httpx: Any = httpx

    def __init__(
        self,
        url: str | None = None,
        *,
        target: Callable[[OutboxEvent], WebhookTarget | Awaitable[WebhookTarget]] | None = None,
        secret: str | bytes | None = None,
        timeout_seconds: float = DEFAULT_WEBHOOK_TIMEOUT_SECONDS,
        headers: Mapping[str, str] | None = None,
        allows_url: Callable[[str], bool | Awaitable[bool]] | None = None,
        signature_header: str = WEBHOOK_SIGNATURE_HEADER,
        result_field: str | None = None,
    ) -> None:
        PackageChecks.require_package(self.httpx, "WebhookDelivery", "httpx", "http")
        if (url is None) == (target is None):
            raise ConfigurationError("WebhookDelivery takes url or target, one of them")
        if target is not None and secret is not None:
            raise ConfigurationError("WebhookDelivery takes the secret of a target from its WebhookTarget")
        self.url = url
        self.target = target
        self.secret = secret
        self.timeout_seconds = timeout_seconds
        self.headers = dict(headers or {})
        self.allows_url = allows_url
        self.signature_header = signature_header
        self.result_field = result_field
        self.client: Any = None

    def get_client(self) -> Any:
        """The HTTP client, opened on first use.

        Returns:
            The ``httpx.AsyncClient``.
        """
        if self.client is None:
            self.client = self.httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=False)
        return self.client

    @staticmethod
    def get_signature(body: bytes, secret: str | bytes) -> str:
        """The signature header value of a body.

        Args:
            body: The body.
            secret: The key it is signed with.

        Returns:
            ``sha256=<hex digest>``.
        """
        key = secret.encode() if isinstance(secret, str) else secret
        return WEBHOOK_SIGNATURE_PREFIX + hmac.new(key, body, hashlib.sha256).hexdigest()

    async def get_target(self, event: OutboxEvent) -> WebhookTarget:
        """Where an event goes - ``url`` with ``secret``, or what ``target`` gives.

        Args:
            event: The event.

        Returns:
            The target.
        """
        if self.target is None:
            assert self.url is not None  # nosec B101
            return WebhookTarget(self.url, self.secret)
        target = self.target(event)
        if inspect.isawaitable(target):
            target = await target
        return target

    async def is_url_allowed(self, url: str) -> bool:
        """Whether ``allows_url`` lets a URL be requested.

        Args:
            url: The URL.

        Returns:
            False when it refuses the URL.
        """
        if self.allows_url is None:
            return True
        allowed = self.allows_url(url)
        if inspect.isawaitable(allowed):
            allowed = await allowed
        return bool(allowed)

    async def write_result(
        self, event: OutboxEvent, status_code: int | None, body: str | None, error: str | None
    ) -> None:
        """Writes an attempt's result to ``result_field`` of the event's row.

        Args:
            event: The event.
            status_code: The response's status, None without a response.
            body: The response's body, None without a response.
            error: Why the attempt failed before a response, None with one.
        """
        if self.result_field is None:
            return
        result = {
            "status_code": status_code,
            "body": WebhookDelivery.get_result_text(body),
            "error": WebhookDelivery.get_result_text(error),
        }
        model = type(event)
        # The relay serves every tenant's events - its writes are of the whole table.
        queryset = model.objects.all_tenants() if model._meta.tenant_field else model.objects.all()
        await queryset.filter(pk=event.pk).update(**{self.result_field: result})

    @staticmethod
    def get_result_text(text: str | None) -> str | None:
        """A response body or an error as the result field holds it - cut to
        ``WEBHOOK_RESULT_BODY_MAX_LENGTH``, a NUL replaced: a write refused after the request was sent
        would send the event again.

        Args:
            text: The text, None for none.

        Returns:
            The text written.
        """
        if text is None:
            return None
        return text[:WEBHOOK_RESULT_BODY_MAX_LENGTH].replace("\x00", WEBHOOK_RESULT_NUL_REPLACEMENT)

    async def deliver(self, event: OutboxEvent) -> None:
        target = await self.get_target(event)
        if not await self.is_url_allowed(target.url):
            await self.write_result(event, None, None, WEBHOOK_URL_REFUSED_ERROR)
            raise DeliveryError(f"the webhook URL of outbox event {event.id} is refused")
        body = self.get_body(event)
        headers = {**self.headers, **target.headers, **self.get_headers(event), "content-type": "application/json"}
        if target.secret is not None:
            headers[self.signature_header] = self.get_signature(body, target.secret)
        for name in headers:
            if not name.isascii():
                raise DeliveryError(f"a webhook header name is ASCII, got {name!r}")
        try:
            response = await self.get_client().post(
                target.url, content=body, headers={name: value.encode() for name, value in headers.items()}
            )
        except self.httpx.HTTPError as error:
            await self.write_result(event, None, None, str(error) or type(error).__name__)
            raise DeliveryError(f"the webhook request of outbox event {event.id} failed: {error!r}") from error
        await self.write_result(event, response.status_code, response.text, None)
        if not 200 <= response.status_code < 300:
            raise DeliveryError(f"the webhook answered {response.status_code} for outbox event {event.id}")

    async def close(self) -> None:
        if self.client is not None:
            client, self.client = self.client, None
            await client.aclose()
