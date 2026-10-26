"""OutboxRelay constructor options - the batch size, which goes to LIMIT, is checked."""

import pytest

from hare.contrib.outbox import OutboxRelay
from hare.contrib.outbox.relay.constants import MAX_BATCH_SIZE
from hare.exceptions import ConfigurationError
from tests.contrib.outbox.models import DemoOutboxEvent

pytestmark = pytest.mark.database_independent


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
        {"name": "relay-1"},
    ],
)
def test_boundary_options_are_accepted(options):
    relay = OutboxRelay(DemoOutboxEvent, deliver, **options)
    option_name, option_value = next(iter(options.items()))
    assert getattr(relay, option_name) == option_value


@pytest.mark.parametrize("attempts", [1, 1024, 1025, 10_000])
def test_the_retry_pause_never_overflows(attempts):
    relay = OutboxRelay(DemoOutboxEvent, deliver, retry_base_seconds=1.0, retry_max_seconds=300.0)
    delay = relay.get_retry_delay_seconds(attempts)
    assert 0 < delay <= 300.0 * 1.1
