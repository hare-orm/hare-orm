from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from hare.fields.constants import ROLLBACK_RESTORE_UNSET
from hare.instrumentation.change_events import ChangeEvents
from hare.instrumentation.enums import RowOperation
from hare.models.constants import WRITE_RESTORE_FAILURES_MESSAGE
from hare.models.instances.dirty_fields import DirtyFields
from hare.models.write.instance_snapshot import InstanceSnapshot
from hare.models.write.rollback_restores import RollbackRestores
from hare.models.write.rules.written_instances import WrittenInstances
from hare.models.write.write_steps import WriteSteps

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models.model import Model
    from hare.models.write.declarations import WriteOutcome
    from hare.models.write.instance_values import InstanceValues


class WriteFinalizer:
    """Publish a known write outcome and preserve the preceding persistence state.

    Database execution, instance mutation and change reporting are separate steps.
    Unknown row counts keep their meaning; an absent statement does not emit a write
    event. Rollback bookkeeping is attached to the client that actually performed it.
    """

    @staticmethod
    def remember_persistence(obj: Model, connection: DatabaseClient) -> InstanceSnapshot:
        """Record persistence flags before field setters can change them.

        Register before returned keys and stamps, so reverse undo restores flags
        last. Repeated registration in the same savepoint keeps the first snapshot.
        """
        before = InstanceSnapshot.capture_persistence(obj)
        WriteFinalizer.register_snapshots((before,), connection)
        return before

    @staticmethod
    def register_snapshots(snapshots: Iterable[InstanceSnapshot], connection: DatabaseClient) -> None:
        """Attach persistence undo to the exact client, before publishing fields."""
        if connection.is_transaction_client:
            RollbackRestores.register_snapshots(connection, snapshots)

    @staticmethod
    def settle_values(values: InstanceValues, connection: DatabaseClient, outcome: WriteOutcome) -> None:
        """Keep automatic assignments for a completed write, undo a known non-write."""
        if not outcome.performed or outcome.row_count == 0:
            values.restore()
        else:
            values.keep(connection)

    @staticmethod
    def restore_failed_values(
        values: InstanceValues, error: BaseException, instances: Iterable[Model] | None = None
    ) -> None:
        """Undo preparation without replacing the original error if a setter also fails."""
        try:
            values.restore(instances)
        except BaseException as restore_error:
            raise BaseExceptionGroup(WRITE_RESTORE_FAILURES_MESSAGE, [error, restore_error]) from None

    @staticmethod
    def publish_saved(
        snapshots: Sequence[InstanceSnapshot],
        connection: DatabaseClient,
        outcome: WriteOutcome,
        *,
        updated_fields: Sequence[str] | None = None,
    ) -> None:
        """Mark saved instances of one model and update only the dirty baseline of written fields.

        The caller has checked a known zero-row update or verified existence for a
        no-op save. Each complete persistence snapshot is retained for a later rollback,
        including the connection alias and generated-key flag changed by publication.
        """
        if not snapshots:
            return
        objs = [snapshot.instance for snapshot in snapshots]
        WrittenInstances.mark_saved(objs, connection)
        if outcome.operation is RowOperation.INSERT:
            # A missing per-instance flag reads the model's False class default.
            WrittenInstances.clear_custom_generated_keys(
                [
                    snapshot.instance
                    for snapshot in snapshots
                    if snapshot.values["_custom_generated_pk"] is ROLLBACK_RESTORE_UNSET
                    or not snapshot.values["_custom_generated_pk"]
                ]
            )
        if objs[0]._meta.track_dirty_fields:
            for obj in objs:
                if updated_fields is None:
                    DirtyFields.snapshot_dirty_fields(obj)
                else:
                    DirtyFields.sync_dirty_snapshot_fields(obj, updated_fields)

    @staticmethod
    async def saved(
        before: InstanceSnapshot,
        connection: DatabaseClient,
        outcome: WriteOutcome,
        *,
        updated_fields: Sequence[str] | None = None,
        reported_fields: Sequence[str] | None = None,
    ) -> None:
        """Publish one saved instance and report an actual statement to listeners.

        Bulk callers publish through ``publish_saved`` and report their entire batch
        through the bulk reporting contract, without emitting one event per object.
        """
        WriteFinalizer.publish_saved((before,), connection, outcome, updated_fields=updated_fields)
        obj = before.instance
        if outcome.performed and ChangeEvents.is_observed(type(obj)):
            await WriteSteps.report(
                connection,
                type(obj),
                outcome.operation,
                instances=[obj],
                fields=None if outcome.operation is RowOperation.INSERT else reported_fields,
            )
