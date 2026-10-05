"""OutboxRelay constructor option validation - type and range of every option."""

import math

import pytest

from hare.contrib.outbox import OutboxRelay
from hare.contrib.outbox.constants import MAX_INTERVAL_SECONDS
from hare.contrib.outbox.relay.constants import MAX_BATCH_SIZE, MAX_CONCURRENCY, MAX_DELIVERY_ATTEMPTS_LIMIT
from hare.exceptions import ConfigurationError
from tests.contrib.outbox.models import DemoOutboxEvent


async def deliver(event: DemoOutboxEvent) -> None:
    pass


@pytest.mark.parametrize(
    "options",
    [
        {"batch_size": 0},
        {"batch_size": MAX_BATCH_SIZE + 1},
        {"batch_size": 1.5},
        {"batch_size": True},
        {"batch_size": "10"},
        {"max_delivery_attempts": 0},
        {"max_delivery_attempts": MAX_DELIVERY_ATTEMPTS_LIMIT + 1},
        {"max_delivery_attempts": 2.0},
        {"concurrency": 0},
        {"concurrency": MAX_CONCURRENCY + 1},
        {"poll_interval_seconds": 0},
        {"poll_interval_seconds": -1},
        {"poll_interval_seconds": math.inf},
        {"poll_interval_seconds": math.nan},
        {"poll_interval_seconds": MAX_INTERVAL_SECONDS + 1},
        {"poll_interval_seconds": "5"},
        {"retry_base_seconds": -0.1},
        {"retry_max_seconds": MAX_INTERVAL_SECONDS + 1},
        {"lease_seconds": 0},
        {"delivery_timeout_seconds": 0},
        {"topics": "widget"},
        {"topics": []},
        {"topics": [""]},
        {"name": ""},
        {"name": "x" * 256},
    ],
)
def test_invalid_options_raise_configuration_error(options):
    with pytest.raises(ConfigurationError, match=next(iter(options))):
        OutboxRelay(DemoOutboxEvent, deliver, **options)


def test_options_that_contradict_each_other_are_refused():
    with pytest.raises(ConfigurationError, match="retry_max_seconds"):
        OutboxRelay(DemoOutboxEvent, deliver, retry_base_seconds=10, retry_max_seconds=5)
    with pytest.raises(ConfigurationError, match="delivery_timeout_seconds"):
        OutboxRelay(DemoOutboxEvent, deliver, lease_seconds=10, delivery_timeout_seconds=10)
    with pytest.raises(ConfigurationError, match="delivery"):
        OutboxRelay(DemoOutboxEvent, 42)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "options",
    [
        {"batch_size": 1},
        {"batch_size": MAX_BATCH_SIZE},
        {"max_delivery_attempts": 1},
        {"max_delivery_attempts": MAX_DELIVERY_ATTEMPTS_LIMIT},
        {"concurrency": MAX_CONCURRENCY},
        {"poll_interval_seconds": 0.01},
        {"poll_interval_seconds": MAX_INTERVAL_SECONDS},
        {"retry_base_seconds": 0},
        {"name": "relay-1"},
    ],
)
def test_boundary_options_are_accepted(options):
    relay = OutboxRelay(DemoOutboxEvent, deliver, **options)
    option_name, option_value = next(iter(options.items()))
    assert getattr(relay, option_name) == option_value
