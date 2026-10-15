"""What the ORM reports to its observers and query wrappers - declarations only."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, ClassVar

from hare.instrumentation.enums import PoolRole, RowOperation
from hare.transactions.enums import TransactionEventType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model


@dataclasses.dataclass(frozen=True, slots=True)
class QueryCall:
    """A query-executing call a ``QueryWrapper`` runs around.

    Attributes:
        method_name: The client method - ``execute``, ``execute_many``, ``execute_script``,
            ``execute_described``, ``copy`` or ``stream``.
        sql: The statement text; ``COPY <table> (<columns>) FROM STDIN`` for a bulk load.
        parameters: The bound parameters, None when there are none to show.
        connection_alias: The connection it runs on.
        dialect: The connection's dialect.
    """

    method_name: str
    sql: str
    parameters: list[Any] | None
    connection_alias: str
    dialect: Dialect


@dataclasses.dataclass(frozen=True, slots=True)
class QueryExecuted:
    """A statement sent to the database - every query-executing call (``execute``,
    ``execute_many``, ``execute_script``, a bulk ``COPY``, a stream), successful or not.

    Attributes:
        sql: The statement text, its query tags included; ``COPY <table> (<columns>) FROM STDIN``
            for a bulk load.
        parameters: The bound parameters, None when there are none to show.
        duration_ms: How long the call took - for a stream, until it was read to the end or
            closed.
        error: The exception the call ended with, None on success.
        connection_alias: The connection it ran on.
    """

    #: Observers of this event can't be narrowed to models.
    observed_by_model: ClassVar[bool] = False

    sql: str
    parameters: list[Any] | None
    duration_ms: float
    error: Exception | None
    connection_alias: str


@dataclasses.dataclass(frozen=True, slots=True)
class RowsChanged:
    """Rows of one model a committed write changed - see ``ChangeEvents`` for which writes report.

    Attributes:
        model: The model.
        operation: Inserted, updated or deleted.
        pks: The primary keys of the rows; None when the write names no rows - a
            ``QuerySet.update()``/``delete()`` of a model without ``Meta.change_capture``, the rows
            ``on_delete`` reaches, a model without a primary key.
        fields: The fields the write set; None when it isn't known - an insert, a ``save()``
            without ``update_fields``.
        connection_alias: The connection the write ran on.
    """

    #: Observers of this event can be narrowed to models - and their subclasses.
    observed_by_model: ClassVar[bool] = True

    model: type[Model]
    operation: RowOperation
    pks: tuple[Any, ...] | None
    fields: tuple[str, ...] | None
    connection_alias: str


@dataclasses.dataclass(frozen=True, slots=True)
class TransactionEvent:
    """A real, top-level transaction began, committed or rolled back - never a savepoint.

    Attributes:
        type: Begin, commit or rollback.
        connection_alias: The connection the transaction runs on.
        duration_ms: 0.0 for a begin; the time since the begin for a commit or rollback.
        error: The error a rollback was caused by - a lost connection carries the connection
            error (when it broke while the COMMIT was in flight, the COMMIT may still have landed,
            and no ``on_commit()``/``on_rollback()`` callback runs); None otherwise.
    """

    #: Observers of this event can't be narrowed to models.
    observed_by_model: ClassVar[bool] = False

    type: TransactionEventType
    connection_alias: str
    duration_ms: float
    error: Exception | None


@dataclasses.dataclass(frozen=True, slots=True)
class PoolStatus:
    """What one pool of connections holds and has done, at one moment.

    Attributes:
        connection_alias: The connection of the configuration.
        role: Which client of the connection the pool belongs to.
        schema: The tenant schema of a tenant schema's pool, None otherwise.
        size: The open connections.
        idle: The open connections nobody uses.
        in_use: The connections taken.
        waiting: The tasks waiting for a connection.
        min_size: The connections the pool keeps open.
        max_size: The most connections the pool opens.
        acquire_count: How many times a connection was taken.
        acquire_timeouts: How many times waiting for a connection ran out of ``pool_acquire_timeout``.
        acquire_wait_seconds_total: How long the waits for a connection took in all - measured only
            while the pool metrics are enabled (``PoolMetrics``).
        connect_count: How many connections the pool opened.
        connect_failures: How many times opening a connection failed.
    """

    connection_alias: str
    role: PoolRole
    schema: str | None
    size: int
    idle: int
    in_use: int
    waiting: int
    min_size: int
    max_size: int
    acquire_count: int
    acquire_timeouts: int
    acquire_wait_seconds_total: float
    connect_count: int
    connect_failures: int


@dataclasses.dataclass(frozen=True, slots=True)
class PoolAcquireTimedOut:
    """Waiting for a connection of a pool ran out of ``pool_acquire_timeout`` - the call raised
    ``PoolTimeoutError``.

    Attributes:
        connection_alias: The connection of the configuration.
        role: Which client of the connection the pool belongs to.
        schema: The tenant schema of a tenant schema's pool, None otherwise.
        waited_seconds: How long the call waited.
        size: The open connections of the pool at that moment.
        max_size: The most connections the pool opens.
        waiting: The tasks waiting for a connection at that moment.
    """

    #: Observers of this event can't be narrowed to models.
    observed_by_model: ClassVar[bool] = False

    connection_alias: str
    role: PoolRole
    schema: str | None
    waited_seconds: float
    size: int
    max_size: int
    waiting: int


@dataclasses.dataclass(frozen=True, slots=True)
class ConnectionFailed:
    """Opening a connection to the database failed - opening a pool, or a pool growing.

    Attributes:
        connection_alias: The connection of the configuration.
        role: Which client of the connection the pool belongs to.
        address: The server's address (``host:port``, or the SQLite file).
        error_type: The name of the exception's class - its text may hold the server's address and
            user, and stays with the exception.
        attempt: The attempt of opening the pool, from 1; 1 for a pool growing.
        will_retry: Whether another attempt follows.
    """

    #: Observers of this event can't be narrowed to models.
    observed_by_model: ClassVar[bool] = False

    connection_alias: str
    role: PoolRole
    address: str
    error_type: str
    attempt: int
    will_retry: bool
