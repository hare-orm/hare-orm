"""A ClickHouse client opens its driver's connection on first use - once, however many statements
start together."""

import asyncio
import time

import pytest

from hare import Connections
from hare.exceptions import ConfigurationError, OperationalError


@pytest.mark.asyncio
async def test_statements_starting_together_open_one_connection(clickhouse_db):
    client = Connections.current().create_independent("models", {})
    opened = []
    create_connection = client.create_connection

    async def counting_create_connection(with_db: bool) -> None:
        opened.append(with_db)
        await create_connection(with_db)

    client.create_connection = counting_create_connection
    try:
        results = await asyncio.gather(*[client.execute_dicts(f"SELECT {number} AS n") for number in range(8)])
        assert [rows[0]["n"] for rows in results] == list(range(8))
        assert opened == [True]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_statement_running_past_the_timeout_is_stopped_by_the_server(clickhouse_db):
    """The native protocol's timeout bounds a wait for the server's next packet, which a running
    statement keeps sending - the server stops the statement itself (max_execution_time)."""
    client = Connections.current().create_independent("models", {"send_receive_timeout": 1})
    try:
        started = time.monotonic()
        with pytest.raises(OperationalError, match="Timeout exceeded|159"):
            await client.execute_dicts("SELECT sleep(2.5) AS slept")
        assert time.monotonic() - started < 2
    finally:
        await client.close()


def test_keeper_hosts_without_transactions_is_refused():
    """The Keeper locks the rows of a transaction - a connection without transactions would lock none."""
    from hare.dialects.clickhouse.drivers.clickhouse_connect.client.clickhouse_connect_client import (
        ClickhouseConnectClient,
    )

    with pytest.raises(ConfigurationError, match="needs transactions=true"):
        ClickhouseConnectClient(connection_alias="default", host="127.0.0.1", keeper_hosts="127.0.0.1:9181")
    ClickhouseConnectClient(
        connection_alias="default", host="127.0.0.1", keeper_hosts="127.0.0.1:9181", transactions=True
    )
