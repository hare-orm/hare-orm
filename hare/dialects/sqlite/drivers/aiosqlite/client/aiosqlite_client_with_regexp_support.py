from __future__ import annotations

from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_client import AiosqliteClient
from hare.dialects.sqlite.functions.regex.sqlite_posix_regex import SqlitePosixRegex


class AiosqliteClientWithRegexpSupport(AiosqliteClient):
    """The aiosqlite client of ``install_regexp_functions=true`` - each connection gets the regular expression
    functions of the POSIX regex lookups."""

    features = AiosqliteClient.features.replace(supports_posix_regex=True)

    async def _post_connect(self) -> None:
        await super()._post_connect()
        if self._connection:
            await SqlitePosixRegex.install(self._connection, self.function_errors)
