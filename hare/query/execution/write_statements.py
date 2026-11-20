from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.dialects.base.client.statement_recovery import StatementRecovery
    from hare.fields.field import Field


@dataclass(frozen=True, slots=True)
class BoundWriteValue:
    """A field assignment converted once for one execution, before SQL compilation.

    Keeping the Python value alongside the database value lets write-plan consumers
    distinguish a prepared assignment from a raw expression literal. Randomized
    encryption and other conversion effects are not repeated by a plan hit or rebuild.
    """

    field: Field[Any]
    value: Any
    database_value: Any


@dataclass(frozen=True, slots=True)
class WriteStatement:
    """SQL and already converted parameters for exactly one driver execution."""

    sql: str
    parameters: list[Any]
    returns_rows: bool = False
    recovery: StatementRecovery | None = None


@dataclass(frozen=True, slots=True)
class WriteMany:
    """One SQL template executed with each of the already converted rows, in order."""

    sql: str
    rows: list[list[Any]]
    recovery: StatementRecovery | None = None


@dataclass(frozen=True, slots=True)
class WriteCopy:
    """A bulk load with its destination, column types and converted rows fixed."""

    table: str
    columns: list[str]
    rows: list[tuple[Any, ...]]
    column_types: list[str]
    schema: str | None


WriteCommand = WriteStatement | WriteMany | WriteCopy


@dataclass(frozen=True, slots=True)
class WritePreview:
    """Commands available without running defaults or allocating database values.

    ``commands=None`` and ``requires_transaction=None`` mean preparation is deferred;
    ``deferred_fields`` names the values still needed. An empty command tuple means
    a known empty write, not an unknown plan. Parameters are a preview snapshot and
    are never reserved for a later execution of the original query.
    """

    commands: tuple[WriteCommand, ...] | None
    requires_transaction: bool | None
    deferred_fields: tuple[str, ...] = ()
