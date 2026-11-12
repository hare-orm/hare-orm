from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.dialects.base.client.transaction_client import TransactionClient
    from hare.dialects.base.transactions.savepoints.savepoint_span import SavepointSpan
    from hare.instrumentation.enums import RowOperation
    from hare.models.write.instance_snapshot import InstanceSnapshot
    from hare.models.write.rules.row_write_rules import RowWriteRules


@dataclass(frozen=True, slots=True)
class InstanceWriteTemplates:
    """Reusable single-instance SQL shapes, separate from values of a particular write.

    The generated-key and explicit-key INSERT variants have immutable ordered field
    lists. DELETE variants distinguish ordinary visibility from a cascade's matched
    descendant. The model cache controls the lifetime of the complete declaration;
    a writer never reconstructs it through a positional attribute protocol.
    """

    insert_fields: tuple[str, ...]
    insert_sql: str
    insert_returns_rows: bool
    explicit_key_fields: tuple[str, ...]
    explicit_key_sql: str
    explicit_key_returns_rows: bool
    delete_sql: str
    delete_without_tenant_scope_sql: str
    insert_rules: RowWriteRules
    update_rules: RowWriteRules


@dataclass(eq=False, slots=True)
class RollbackRestoreLayer:
    """One model's undo record for an exact transaction and savepoint lifetime.

    The client is the top-level transaction that receives the callbacks. Savepoint
    identity belongs to that client's lock; a span from another connection cannot
    combine unrelated writes. Layers are removed by identity, never by value.
    """

    client: TransactionClient
    span: SavepointSpan | None
    snapshots: list[InstanceSnapshot]
    field_names: set[str] | None = None


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    """Observed outcome of a completed write, separate from instance publication.

    ``row_count=None`` means the driver does not know the count. ``performed=False``
    means no statement was sent, such as an instance update with no writable fields.
    Neither condition is interchangeable with an executed statement matching zero rows.
    """

    operation: RowOperation
    row_count: int | None
    performed: bool = True
