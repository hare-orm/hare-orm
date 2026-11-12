from __future__ import annotations

from types import TracebackType
from typing import Self

from hare.core.resource_completion import ResourceCompletion
from hare.dialects.base.schema.constants import SCHEMA_CLEANUP_FAILED
from hare.dialects.base.schema.shadow.shadow_database import ShadowDatabase
from hare.dialects.sqlite.client.constants import SQLITE_ATTACHED_DATABASES_OPTION_NAME
from hare.dialects.sqlite.constants import SQLITE_IN_MEMORY_FILENAME


class SqliteShadowDatabase(ShadowDatabase):
    """ShadowDatabase as SQLite makes it: a database in memory, opened with the connection's settings -
    the functions and extensions it loads alike, a database in memory under the name of each one the
    connection attaches - and gone when it is closed."""

    async def __aenter__(self) -> Self:
        attached_databases = self.connection.attached_databases  # type: ignore[attr-defined]
        self.client = self.connection.create_independent_client(
            {
                "file_path": SQLITE_IN_MEMORY_FILENAME,
                SQLITE_ATTACHED_DATABASES_OPTION_NAME: dict.fromkeys(attached_databases, SQLITE_IN_MEMORY_FILENAME),
            }
        )
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.client is not None:
            try:
                await ResourceCompletion.wait(self.client.close())
            except BaseException as cleanup_error:
                if exception is not None:
                    raise BaseExceptionGroup(SCHEMA_CLEANUP_FAILED, [exception, cleanup_error]) from None
                raise
