"""Change capture on ClickHouse: the outbox declares unique fields, which ClickHouse keeps nothing of - the
outbox, and with it every captured model, is refused when bound."""

import os

import pytest

from hare.contrib.test import hare_test_context
from hare.exceptions import ConfigurationError


@pytest.mark.asyncio
async def test_the_outbox_is_refused_when_bound():
    db_url = os.getenv("HARE_TEST_DB", "")
    if not db_url.startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")
    with pytest.raises(ConfigurationError, match="keeps nothing unique"):
        async with hare_test_context(
            modules=["tests.dialects.clickhouse.change_capture.models"],
            db_url=db_url,
            app_label="models",
            connection_label="models",
        ):
            pass
