from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True, slots=True)
class SqliteDriverErrors:
    """The exception classes a SQLite driver raises - what the shared client translates into hare's
    own. Read only when a statement fails.

    Attributes:
        operational: A statement the database refused or couldn't finish.
        integrity: A broken constraint.
        interface: The connection or cursor became unusable.
        statement: The other errors of a statement - a misuse of the API, a value of the wrong
            type, an internal error, an unsupported feature.
        database: Any other error of the database - a corrupted file.
        closed_connection_message: The text of the ``ValueError`` the driver raises for a call on a
            closed connection; None when it raises none.
    """

    operational: type[Exception]
    integrity: type[Exception]
    interface: type[Exception]
    statement: tuple[type[Exception], ...]
    database: type[Exception]
    closed_connection_message: str | None
