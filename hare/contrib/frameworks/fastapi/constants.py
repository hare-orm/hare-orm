from __future__ import annotations

#: The ASGI scopes that start a request's own state.
REQUEST_SCOPE_TYPES = frozenset({"http", "websocket"})
