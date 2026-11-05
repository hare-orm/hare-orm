"""What the database clients report - declarations only."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.dialects.base.client.statement_recovery import StatementRecovery


@dataclasses.dataclass(frozen=True, slots=True)
class StatementRequest:
    """One driver invocation and its explicit permission to retry a read.

    Parameters retain their sensitive-value metadata. A request is local to this
    invocation: neither observers nor nested statements inherit its retry permission.
    """

    sql: str | None
    parameters: Any = None
    retry_read: bool = False
    recovery: StatementRecovery | None = None


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
