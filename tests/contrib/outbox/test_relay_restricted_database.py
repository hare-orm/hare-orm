"""The relay leases its events with one UPDATE ... RETURNING reading a correlated subquery - on a
database lacking any of them it refuses to start instead of failing on every poll."""

import pytest

from hare.contrib.outbox import OutboxRelay
from hare.exceptions import ConfigurationError
from tests.contrib.outbox.models import DemoOutboxEvent


async def no_op_deliver(_event: DemoOutboxEvent) -> None:
    pass


@pytest.mark.parametrize(
    "missing_feature", ["supports_row_updates", "supports_returning", "supports_correlated_subqueries"]
)
@pytest.mark.asyncio
async def test_the_relay_refuses_to_start_on_a_database_that_cant_lease(db_outbox, monkeypatch, missing_feature):
    connection = DemoOutboxEvent.get_connection(for_write=True)
    monkeypatch.setattr(connection, "features", connection.features.replace(**{missing_feature: False}))
    relay = OutboxRelay(DemoOutboxEvent, no_op_deliver)
    with pytest.raises(ConfigurationError, match="leases its events with one UPDATE ... RETURNING"):
        await relay.start()
    assert relay.task is None
