from __future__ import annotations

import os

import pytest

from hare.dialects.base.client.database_client import DatabaseClient


class PostgresqlExtensions:
    """Check server packages independently of extension installation or schema generation.

    Release profiles require their extensions. A local run can explicitly allow an
    absent package with HARE_TEST_OPTIONAL_POSTGRES_EXTENSIONS, a comma-separated list.
    Only absence from pg_available_extensions can skip; connection, permission and
    installation errors always fail. CI ignores the local opt-out.
    """

    @staticmethod
    async def require(client: DatabaseClient, name: str) -> None:
        rows = await client.execute_dicts("SELECT name FROM pg_available_extensions WHERE name = $1", [name])
        if rows:
            return
        optional = set(os.getenv("HARE_TEST_OPTIONAL_POSTGRES_EXTENSIONS", "").split(","))
        if not os.getenv("CI") and name in optional:
            pytest.skip(f"Optional local PostgreSQL extension {name!r} is absent from pg_available_extensions")
        pytest.fail(f"Required PostgreSQL extension {name!r} is absent from pg_available_extensions")
