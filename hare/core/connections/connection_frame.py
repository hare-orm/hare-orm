from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.core.connections.connection_token import ConnectionToken
    from hare.dialects.base.client.database_client import DatabaseClient


@dataclass(frozen=True, slots=True)
class ConnectionFrame:
    """Immutable transaction bindings for one application's execution context.

    A child task inherits this frame. Replacing an alias creates another frame; ending a
    transaction marks its token released so inherited frames cannot reuse a finished client.
    Shared clients belong to ConnectionHandler and are never copied into this frame.
    """

    application: object
    bindings: Mapping[str, ConnectionToken] = field(default_factory=lambda: MappingProxyType({}))

    def get(self, alias: str) -> DatabaseClient | None:
        token = self.get_binding(alias)
        return None if token is None else token._new_value

    def get_binding(self, alias: str) -> ConnectionToken | None:
        """Skip completed leases without mutating an inherited frame."""
        token = self.bindings.get(alias)
        while token is not None:
            if not token._used:
                return token
            token = token._previous
        return None

    def with_binding(self, alias: str, token: ConnectionToken) -> ConnectionFrame:
        return ConnectionFrame(self.application, MappingProxyType({**self.bindings, alias: token}))

    def without(self, alias: str) -> ConnectionFrame:
        return ConnectionFrame(
            self.application, MappingProxyType({name: token for name, token in self.bindings.items() if name != alias})
        )
