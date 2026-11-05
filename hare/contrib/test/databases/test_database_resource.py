from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient


class TestDatabaseResource:
    """The acquired client used to create, populate and release one test database."""

    def __init__(self, client: DatabaseClient) -> None:
        self.client = client

    async def create(self) -> None:
        await self.client.db_create()

    async def generate_schema(self, safe: bool = False) -> None:
        await self.client.generate_schema(safe)

    async def release(self) -> None:
        await self.client.db_delete()
