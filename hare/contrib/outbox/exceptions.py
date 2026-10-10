from __future__ import annotations


class DeliveryError(Exception):
    """An outbox event wasn't delivered - a delivery's own refusal (a webhook answering an error, no
    route taking the topic), or the failure the relay logs, the delivery's exception chained as
    ``__cause__``."""
