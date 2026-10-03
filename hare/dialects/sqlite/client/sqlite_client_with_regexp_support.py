from hare.dialects.sqlite.client.sqlite_client import SqliteClient
from hare.dialects.sqlite.functions.regex import SqlitePosixRegex


class SqliteClientWithRegexpSupport(SqliteClient):
    features = SqliteClient.features.replace(supports_posix_regex=True)

    async def create_connection(self, with_db: bool) -> None:
        await super().create_connection(with_db)
        if self._connection:
            await SqlitePosixRegex.install(self._connection)
