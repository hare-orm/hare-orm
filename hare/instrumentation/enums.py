from __future__ import annotations

from enum import StrEnum


class RowOperation(StrEnum):
    """What a write did to the rows of a model."""

    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"


class ChangePayload(StrEnum):
    """What a captured change holds of its rows (``Meta.change_capture``)."""

    #: The primary key only.
    KEYS = "keys"
    #: The row as the write left it - as it was before for a delete.
    AFTER = "after"
    #: The row as it was before the write and as the write left it.
    BEFORE_AND_AFTER = "before_and_after"


class PoolRole(StrEnum):
    """Which client of a connection a pool belongs to."""

    #: The connection's own client.
    OWN = "own"
    #: The client of one tenant's schema.
    TENANT_SCHEMA = "tenant_schema"
    #: The client of the server itself, past a transaction pooler (``direct_host``).
    DIRECT = "direct"
    #: A client of its own - ``Transactions.autonomous()``, a session with a lock timeout.
    INDEPENDENT = "independent"
