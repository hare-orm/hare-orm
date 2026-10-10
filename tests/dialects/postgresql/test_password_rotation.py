"""A rotated password on a live server: the pool opens its new connections with the password the
``password_provider`` gives now - asked right away when the server refused the last one, or in the
background every ``password_refresh_seconds``."""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager

import pytest

from hare.contrib.test import hare_test_context
from hare.core.connections.connections import Connections
from tests.dialects.postgresql.conftest import skip_if_not_postgres


class RotatingPassword:
    """The password a secret store hands out - changed by the test, counted per ask."""

    def __init__(self, password: str) -> None:
        self.password = password
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        return self.password


@asynccontextmanager
async def login_role():
    skip_if_not_postgres()
    async with hare_test_context(
        modules=["tests.read_your_writes_models"],
        db_url=os.environ["HARE_TEST_DB"],
        app_label="models",
        connection_label="models",
    ) as ctx:
        admin = Connections.get("models")
        role = f"hare_rotating_{uuid.uuid4().hex[:12]}"
        await admin.execute_script(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'first'")
        try:
            yield ctx, admin, role
        finally:
            await admin.execute_script(
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{role}'; "
                f'DROP ROLE "{role}"'
            )


def get_server_settings(admin) -> dict:
    """The rotating role logs in to the server itself - a transaction pooler in front of it
    (PgBouncer) authenticates only the users it lists."""
    if not admin.transaction_pooling:
        return {}
    return {
        "host": admin.direct_host,
        "port": admin.direct_port or admin.port,
        "transaction_pooling": False,
        "direct_host": None,
        "direct_port": None,
    }


async def current_user(client) -> str:
    _, rows = await client.execute("SELECT current_user AS name")
    return rows[0]["name"]


@pytest.mark.asyncio
async def test_a_refused_password_is_asked_for_again_and_the_statement_runs():
    async with login_role() as (_, admin, role):
        password = RotatingPassword("first")
        client = Connections.current().create_independent(
            "models",
            {
                **get_server_settings(admin),
                "user": role,
                "password": None,
                "password_provider": password,
                "min_size": 0,
            },
        )
        try:
            assert await current_user(client) == role
            await admin.execute_script(f"ALTER ROLE \"{role}\" PASSWORD 'second'")
            password.password = "second"
            # The open connections stay; a new one is opened with the remembered, now refused, password.
            await client._expire_connections()
            assert await current_user(client) == role
            assert password.calls == 2
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_a_password_older_than_the_refresh_is_replaced():
    async with login_role() as (_, admin, role):
        password = RotatingPassword("first")
        client = Connections.current().create_independent(
            "models",
            {
                **get_server_settings(admin),
                "user": role,
                "password": None,
                "password_provider": password,
                "password_refresh_seconds": 0.05,
                "min_size": 0,
            },
        )
        try:
            assert await current_user(client) == role
            await admin.execute_script(f"ALTER ROLE \"{role}\" PASSWORD 'second'")
            password.password = "second"
            await asyncio.sleep(0.2)
            await client._expire_connections()
            assert await current_user(client) == role
            assert password.calls >= 2
        finally:
            await client.close()
