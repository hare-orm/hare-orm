from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient


class StatementRecovery(Protocol):
    """An explicit failure action compiled together with an ORM statement.

    Implementations retain target identities, never a live model or SQL parser.
    Recovery must preserve the original failure and must not retry the write.
    Raw SQL has no automatic recovery action.
    """

    async def recover(self, client: DatabaseClient, error: BaseException) -> None: ...
