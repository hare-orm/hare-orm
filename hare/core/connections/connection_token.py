from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.core.connections.connection_handler import ConnectionHandler
    from hare.dialects.base.client.database_client import DatabaseClient


@dataclass(slots=True)
class ConnectionToken:
    """
    Token for resetting connection storage modifications.

    Used by transactions to temporarily replace a connection with a transaction client,
    then restore the original connection when the transaction completes.
    """

    _handler: ConnectionHandler
    _alias: str
    _previous: ConnectionToken | None
    _used: bool = field(default=False)
    #: The client set() put in place for ``_alias``.
    _new_value: DatabaseClient | None = field(default=None)

    def reset(self) -> None:
        """Restores the connection storage through the handler that issued this token - which
        stays correct even after the context re-inits or closes its connections, replacing its
        current handler."""
        self._handler.reset(self)
