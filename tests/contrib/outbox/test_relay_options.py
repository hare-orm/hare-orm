"""OutboxRelay constructor option validation."""

import math

import pytest

from hare.contrib.outbox import OutboxRelay
from hare.contrib.outbox.constants import (
    MAX_BACKOFF_BASE_SECONDS,
    MAX_BATCH_SIZE,
    MAX_DELIVERY_ATTEMPTS_LIMIT,
    MAX_POLL_INTERVAL_SECONDS,
)
from hare.exceptions import ConfigurationError
from tests.contrib.outbox.models import DemoOutboxEvent


async def deliver(event: DemoOutboxEvent) -> None:
    pass


@pytest.mark.parametrize(
    "options",
    [
        {"batch_size": 0},
        {"batch_size": -1},
        {"batch_size": MAX_BATCH_SIZE + 1},
        {"batch_size": 1.5},
        {"batch_size": True},
        {"batch_size": "10"},
        {"max_delivery_attempts": 0},
        {"max_delivery_attempts": -3},
        {"max_delivery_attempts": MAX_DELIVERY_ATTEMPTS_LIMIT + 1},
        {"max_delivery_attempts": 2.0},
        {"poll_interval_seconds": -1},
        {"poll_interval_seconds": 0},
        {"poll_interval_seconds": math.inf},
        {"poll_interval_seconds": math.nan},
        {"poll_interval_seconds": MAX_POLL_INTERVAL_SECONDS + 1},
        {"poll_interval_seconds": "5"},
        {"backoff_base_seconds": -0.1},
        {"backoff_base_seconds": MAX_BACKOFF_BASE_SECONDS + 1},
        {"backoff_base_seconds": math.inf},
        {"listen_channel": ""},
        {"listen_channel": 1},
    ],
)
def test_invalid_options_raise_configuration_error(options):
    with pytest.raises(ConfigurationError, match=next(iter(options))):
        OutboxRelay(DemoOutboxEvent, deliver, **options)


@pytest.mark.parametrize(
    "options",
    [
        {"batch_size": 1},
        {"batch_size": MAX_BATCH_SIZE},
        {"max_delivery_attempts": 1},
        {"max_delivery_attempts": MAX_DELIVERY_ATTEMPTS_LIMIT},
        {"poll_interval_seconds": 0.01},
        {"poll_interval_seconds": 1},
        {"poll_interval_seconds": MAX_POLL_INTERVAL_SECONDS},
        {"backoff_base_seconds": 0},
        {"backoff_base_seconds": MAX_BACKOFF_BASE_SECONDS},
        {"listen_channel": "outbox"},
    ],
)
def test_boundary_options_are_accepted(options):
    relay = OutboxRelay(DemoOutboxEvent, deliver, **options)
    option_name, option_value = next(iter(options.items()))
    assert getattr(relay, option_name) == option_value
