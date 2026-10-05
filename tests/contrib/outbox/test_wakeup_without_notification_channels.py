"""The LISTEN/NOTIFY wakeup on a database without notification channels: refused, never a relay waiting
for a signal that can't come."""

import pytest

from hare.contrib.outbox import ListenNotifyWakeup
from hare.contrib.test import assert_query_count, requires_features
from hare.exceptions import ConfigurationError, UnSupportedError
from tests.contrib.outbox.models import DemoOutboxEvent


@requires_features(supports_listen_notify=False)
@pytest.mark.asyncio
async def test_a_signal_of_written_events_is_refused(db_outbox):
    connection = DemoOutboxEvent.get_connection(for_write=True)
    async with assert_query_count(0):
        with pytest.raises(ConfigurationError, match="has none; leave the outbox without it"):
            await ListenNotifyWakeup(channel="hare_test_outbox_refused").signal_written(connection, ["widget.updated"])


@requires_features(supports_listen_notify=False)
@pytest.mark.asyncio
async def test_listening_is_refused(db_outbox):
    wakeup = ListenNotifyWakeup(channel="hare_test_outbox_refused")
    connection_alias = DemoOutboxEvent.get_connection(for_write=True).connection_alias
    with pytest.raises(UnSupportedError, match="has no notification channels"):
        await wakeup.listen(connection_alias)
    await wakeup.close()
