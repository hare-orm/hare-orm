"""What the database clients report - declarations only."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping


@dataclasses.dataclass(frozen=True, slots=True)
class PingResult:
    """How a ping of a connection went.

    Attributes:
        succeeded: Whether the statement ran within the timeout.
        latency_ms: How long it took - None when it failed.
        error_type: The name of the exception's class it failed with, ``TimeoutError`` for a timeout;
            None when it succeeded. The text stays with the exception: it may hold the server's
            address.
    """

    succeeded: bool
    latency_ms: float | None
    error_type: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class ShellCommand:
    """The interactive client of a database a connection opens (``hare dbshell``).

    Attributes:
        arguments: The program and its arguments.
        environment: Variables added to the program's environment - a password goes here, not
            among the arguments other users of the machine see.
    """

    arguments: tuple[str, ...]
    environment: Mapping[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True, slots=True)
class RowLockOutcome:
    """How a transaction took the locks of rows by their names (``DatabaseClient.take_row_locks()``).

    Attributes:
        taken: The names the transaction now holds - those it held already among them.
        busy: The names another transaction holds - of a call not waiting for them.
        waited: The names taken after waiting for another transaction to give them back.
    """

    taken: tuple[str, ...]
    busy: tuple[str, ...]
    waited: tuple[str, ...]
