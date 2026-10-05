from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from hare.contrib.outbox.broker_arguments import BrokerArguments
from hare.contrib.outbox.constants import MAX_INTERVAL_SECONDS
from hare.contrib.outbox.deliveries.constants import (
    DEFAULT_WEBHOOK_TIMEOUT_SECONDS,
    WEBHOOK_SIGNATURE_HEADER,
    WEBHOOK_SIGNATURE_PREFIX,
)
from hare.contrib.outbox.deliveries.outbox_delivery import OutboxDelivery
from hare.contrib.outbox.exceptions import DeliveryError
from hare.exceptions import ConfigurationError
from hare.numbers.finite_numbers import FiniteNumbers

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.contrib.outbox.outbox_event import OutboxEvent

try:
    import httpx
except ImportError:  # pragma: nocoverage
    httpx = None  # type: ignore[assignment]


class WebhookDelivery(OutboxDelivery):
    """Sends events to an HTTP endpoint - a ``POST`` of the payload's JSON with the event's headers,
    signed with HMAC-SHA256 of the body when a ``secret`` is given (``WEBHOOK_SIGNATURE_HEADER``:
    ``sha256=<hex digest>``). A response other than 2xx fails the event. Needs the ``http`` extra.

    Args:
        url: The endpoint.
        secret: The key the body is signed with; None sends it unsigned.
        timeout_seconds: The longest a request may take.
        headers: Headers sent with every request.

    Raises:
        ConfigurationError: An empty ``url`` or ``secret``, a timeout out of range, or the ``httpx``
            package isn't installed.
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
        BrokerArguments.require_package(self.httpx, "WebhookDelivery", "httpx", "http")
        BrokerArguments.require_text("url", url, "URL")
        if secret is not None and (not isinstance(secret, (str, bytes)) or not secret):
            raise ConfigurationError("secret must be None or non-empty text")
        if not FiniteNumbers.is_finite_number(timeout_seconds) or not 0 < timeout_seconds <= MAX_INTERVAL_SECONDS:
            raise ConfigurationError(
                f"timeout_seconds must be a number > 0 and <= {MAX_INTERVAL_SECONDS}, got {timeout_seconds!r}"
            )
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
        if self.secret is not None:
            headers[WEBHOOK_SIGNATURE_HEADER] = self.get_signature(body)
        response = await self.get_client().post(self.url, content=body, headers=headers)
        if not 200 <= response.status_code < 300:
            raise DeliveryError(f"the webhook answered {response.status_code} for outbox event {event.id}")

    async def close(self) -> None:
        if self.client is not None:
            client, self.client = self.client, None
            await client.aclose()
