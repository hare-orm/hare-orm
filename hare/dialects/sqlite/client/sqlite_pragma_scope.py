from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from hare.core.resource_completion import ResourceCompletion
from hare.dialects.sqlite.constants import SQLITE_PRAGMA_RESTORE_FAILED

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient


class SqlitePragmaScope:
    """Restore an internal connection setting even when enabling it is interrupted.

    Names and values come from dialect code, never user SQL. The setting is read before
    changing it unless the caller supplies its configured restoration value. Restoration
    finishes before cancellation propagates, and preserves an earlier operation failure.
    """

    @staticmethod
    @asynccontextmanager
    async def run(
        connection: DatabaseClient, name: str, value: str | int, *, previous: str | int | None = None
    ) -> AsyncGenerator[None]:
        if previous is None:
            result = await connection.execute(f"PRAGMA {name}")
            previous = result.rows[0][0]
        restore = f"PRAGMA {name} = {previous}"
        try:
            await connection.execute(f"PRAGMA {name} = {value}")
            yield
        except BaseException as error:
            try:
                await ResourceCompletion.wait(connection.execute(restore))
            except BaseException as cleanup_error:
                raise BaseExceptionGroup(SQLITE_PRAGMA_RESTORE_FAILED, [error, cleanup_error]) from None
            raise
        else:
            await ResourceCompletion.wait(connection.execute(restore))
