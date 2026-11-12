from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from hare.contrib.outbox.constants import (
    ENQUEUE_ARGUMENT_FIELD_NAMES,
    OUTBOX_KEY_MAX_LENGTH,
    RELAY_MANAGED_FIELD_NAMES,
)
from hare.exceptions import ValidationError

if TYPE_CHECKING:
    from hare.contrib.outbox.outbox_event import OutboxEvent


class OutboxValidation:
    """Checks the values and metadata written into an outbox event."""

    @staticmethod
    def validate_enqueue_options(
        model: type[OutboxEvent],
        topic: Any,
        idempotency_key: Any,
        ordering_key: Any,
        headers: Mapping[str, Any] | None,
        extra_field_values: Mapping[str, Any] | None,
    ) -> None:
        """Checks ``enqueue()``'s arguments - their text lengths are the fields' own checks.

        Raises:
            ValidationError: ``topic``, ``idempotency_key`` or ``ordering_key`` is empty text,
                ``extra_field_values`` sets a field of an argument or of the relay, or ``headers`` isn't
                a mapping of non-empty text names.
        """
        model.validate_keys(topic, idempotency_key, ordering_key)
        extra_field_names = set(extra_field_values or ())
        argument_field_names = sorted(ENQUEUE_ARGUMENT_FIELD_NAMES & extra_field_names)
        if argument_field_names:
            raise ValidationError(
                f"extra_field_values can't set {', '.join(argument_field_names)} - pass them as enqueue() arguments"
            )
        relay_managed_field_names = sorted(RELAY_MANAGED_FIELD_NAMES & extra_field_names)
        if relay_managed_field_names:
            raise ValidationError(
                f"extra_field_values can't set {', '.join(relay_managed_field_names)} - the ORM and "
                "OutboxRelay maintain them"
            )
        model.validate_headers(headers)

    @staticmethod
    def validate_keys(topic: Any, idempotency_key: Any, ordering_key: Any) -> None:
        """Checks an event's topic and keys aren't empty text - their lengths are the fields' own checks.

        Args:
            topic: The topic.
            idempotency_key: The idempotency key, None for none.
            ordering_key: The ordering key, None for none.

        Raises:
            ValidationError: One of them is empty text.
        """
        for name, value in (("topic", topic), ("idempotency_key", idempotency_key), ("ordering_key", ordering_key)):
            if value == "":
                raise ValidationError(f"{name} must be text of 1 to {OUTBOX_KEY_MAX_LENGTH} characters, got ''")

    @staticmethod
    def validate_headers(headers: Mapping[str, Any] | None) -> None:
        """Checks an event's headers - a delivery sends each by its name.

        Args:
            headers: The headers, None for none.

        Raises:
            ValidationError: They aren't a mapping of non-empty text names.
        """
        if headers is not None and (
            not isinstance(headers, Mapping) or not all(isinstance(name, str) and name for name in headers)
        ):
            raise ValidationError(f"headers must be a mapping of non-empty text names, got {headers!r}")
