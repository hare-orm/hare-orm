"""A ClickHouse client opens its driver's connection on first use - once, however many statements
start together."""

import asyncio

import pytest

from hare import Connections


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
