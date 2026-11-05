"""Transaction options must be enforced or refused before the block can run."""

from unittest.mock import AsyncMock

import pytest

from hare.contrib.test import capture_queries, requires_features
from hare.exceptions import UnSupportedError
from hare.transactions import Transactions


@requires_features(supports_transactions=False)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options", ({"read_only": True}, {"statement_timeout": 1}, {"lock_timeout": 1}, {"isolation": "serializable"})
)
async def test_autonomous_rejects_transaction_options_without_transactions(contract_database, monkeypatch, options):
    connection = contract_database.get_connection()
    independent = connection.create_independent_client()
    close = AsyncMock(wraps=independent.close)
    monkeypatch.setattr(independent, "close", close)
    monkeypatch.setattr(connection, "create_independent_client", lambda: independent)
    async with capture_queries() as captured:
        with pytest.raises(UnSupportedError):
            async with Transactions.autonomous(**options):
                pytest.fail("unsupported transaction options reached the caller's block")
    assert captured.count == 0
    close.assert_awaited_once()
