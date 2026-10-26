from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from hare.contrib.outbox.deliveries.constants import (
    DEFAULT_WEBHOOK_TIMEOUT_SECONDS,
    WEBHOOK_SIGNATURE_HEADER,
    WEBHOOK_SIGNATURE_PREFIX,
)
from hare.contrib.outbox.deliveries.outbox_delivery import OutboxDelivery
from hare.contrib.outbox.exceptions import DeliveryError
from hare.contrib.package_checks import PackageChecks

if TYPE_CHECKING:
    from hare.contrib.outbox.outbox_event import OutboxEvent

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]


class WebhookDelivery(OutboxDelivery):
    """Sends events to an HTTP endpoint - a ``POST`` of the payload's JSON with the event's headers,
    signed with HMAC-SHA256 of the body when a ``secret`` is given (``WEBHOOK_SIGNATURE_HEADER``:
    ``sha256=<hex digest>``). A header value is sent as its UTF-8 bytes - the client writes a text
    value only in ASCII. A response other than 2xx fails the event. Needs the ``http`` extra.

    Args:
        url: The endpoint.
        secret: The key the body is signed with; None sends it unsigned.
        timeout_seconds: The longest a request may take.
        headers: Headers sent with every request.

    Raises:
        ConfigurationError: The ``httpx`` package isn't installed.
    """

    #: The ``httpx`` package, None without the ``http`` extra.
    httpx: Any = httpx

    def __init__(
        self,
        url: str,
        *,
        secret: str | bytes | None = None,
        timeout_seconds: float = DEFAULT_WEBHOOK_TIMEOUT_SECONDS,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        PackageChecks.require_package(self.httpx, "WebhookDelivery", "httpx", "http")
        self.url = url
        self.secret = secret.encode() if isinstance(secret, str) else secret
        self.timeout_seconds = timeout_seconds
        self.headers = dict(headers or {})
        self.client: Any = None

    def get_client(self) -> Any:
        """The HTTP client, opened on first use.

        Returns:
            The ``httpx.AsyncClient``.
        """
        if self.client is None:
            self.client = self.httpx.AsyncClient(timeout=self.timeout_seconds)
        return self.client

    def get_signature(self, body: bytes) -> str:
        """The signature header value of a body.

        Args:
            body: The body.

        Returns:
            ``sha256=<hex digest>``.
        """
        assert self.secret is not None  # nosec B101
        return WEBHOOK_SIGNATURE_PREFIX + hmac.new(self.secret, body, hashlib.sha256).hexdigest()

    async def deliver(self, event: OutboxEvent) -> None:
        body = self.get_body(event)
        headers = {**self.headers, **self.get_headers(event), "content-type": "application/json"}
        for name in headers:
            if not name.isascii():
                raise DeliveryError(f"a webhook header name is ASCII, got {name!r}")
        if self.secret is not None:
            headers[WEBHOOK_SIGNATURE_HEADER] = self.get_signature(body)
        response = await self.get_client().post(
            self.url, content=body, headers={name: value.encode() for name, value in headers.items()}
        )
        if not 200 <= response.status_code < 300:
            raise DeliveryError(f"the webhook answered {response.status_code} for outbox event {event.id}")

    async def close(self) -> None:
        if self.client is not None:
            client, self.client = self.client, None
            await client.aclose()
