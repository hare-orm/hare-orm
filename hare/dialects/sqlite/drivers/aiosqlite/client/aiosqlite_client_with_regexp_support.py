from __future__ import annotations

from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_client import AiosqliteClient
from hare.dialects.sqlite.functions.sqlite_posix_regex import SqlitePosixRegex


class AiosqliteClientWithRegexpSupport(AiosqliteClient):
    features = AiosqliteClient.features.replace(supports_posix_regex=True)

    async def create_connection(self, with_db: bool) -> None:
        await super().create_connection(with_db)
        if self._connection:
            await SqlitePosixRegex.install(self._connection)
