from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.base.client.transaction_lifecycle.transaction_callbacks import TransactionCallbacks
from hare.models.write.declarations import RollbackRestoreLayer
from hare.models.write.instance_snapshot import InstanceSnapshot

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.client.transaction_client import TransactionClient
    from hare.dialects.base.transactions.savepoints.savepoint_span import SavepointSpan
    from hare.models.model import Model


class RollbackRestores:
    """The field values a write changed on an instance, restored when its transaction or savepoint
    rolls back - one layer per open savepoint, so a rollback to a savepoint restores only what was
    written after it."""

    @staticmethod
    def register_rollback_restore(obj: Model, using: DatabaseClient, field_name: str, old_value: Any) -> None:
        """Registers ``field_name`` to be set back to ``old_value`` if the transaction or savepoint
        this write runs in rolls back - a ROLLBACK reverts the row, never the obj.
        ``ROLLBACK_RESTORE_UNSET`` means the field wasn't loaded before the write. A no-op outside a
        transaction.

        The obj keeps a stack of layers, one per open savepoint span, each restored by the
        rollback of its own scope. Within a layer the first value registered for a field wins - it
        is the value from before any of the layer's writes.

        A layer whose connection is finalized but which is still on the stack was abandoned without
        its callbacks running (a ``Transactions.distributed()`` participant whose ``COMMIT
        PREPARED`` failed) - it is dropped here.

        Args:
            obj: The model obj.
            using: The connection the write runs on.
            field_name: The written field.
            old_value: Its value before the write - ``ROLLBACK_RESTORE_UNSET`` when it wasn't loaded.
        """
        RollbackRestores.register_rollback_restores(using, ((obj, field_name, old_value),))

    @staticmethod
    def register_rollback_restores(using: DatabaseClient, restores: Iterable[tuple[Model, str, Any]]) -> None:
        """``RollbackRestores.register_rollback_restore()`` of many instances at once - the layers the write opens
        are restored by one rollback callback and dropped by one commit callback, not two per
        instance.

        Args:
            using: The connection the write ran on.
            restores: ``(instance, field name, old value)`` in registration order.
        """
        if not using.is_transaction_client:
            return
        transaction = cast("TransactionClient", using)
        registered_on = transaction._get_top_level_transaction()
        current_span = TransactionCallbacks.get_registration_span(transaction)
        new_layers: list[tuple[Model, RollbackRestoreLayer]] = []
        for instance, field_name, old_value in restores:
            layer, is_new_layer = RollbackRestores.get_rollback_restore_layer(instance, registered_on, current_span)
            if is_new_layer:
                new_layers.append((instance, layer))
            layer.snapshot.remember(field_name, old_value)
        if not new_layers:
            return

        def _on_rollback() -> None:
            snapshots: list[InstanceSnapshot] = []
            for instance, layer in new_layers:
                snapshots.extend(RollbackRestores.take_rollback_snapshots(instance, layer))
            InstanceSnapshot.restore_all(snapshots)

        def _on_commit() -> None:
            for instance, layer in new_layers:
                RollbackRestores.pop_pending_rollback_restore_layer(instance, layer)

        TransactionCallbacks.add_on_rollback_callback(transaction, _on_rollback)
        TransactionCallbacks.add_on_commit_callback(transaction, _on_commit, is_user_callback=False)

    @staticmethod
    def pop_pending_rollback_restore_layer(obj: Model, layer: RollbackRestoreLayer) -> None:
        """Removes ``layer`` from this obj's pending-rollback-restore stack - by identity: two
        layers can hold equal content.

        Args:
            obj: The model obj.
            layer: The layer to remove.
        """
        stack = obj._pending_rollback_restore_stack or []
        for index, entry in enumerate(stack):
            if entry is layer:
                del stack[index]
                return

    @staticmethod
    def get_rollback_restore_layer(
        obj: Model, registered_on: TransactionClient, current_span: SavepointSpan | None
    ) -> tuple[RollbackRestoreLayer, bool]:
        """The layer of this obj's pending-rollback-restore stack a write in ``current_span``
        registers its old values in - the top one when it is of that span, else a new one pushed.
        Layers of finalized connections are dropped first.

        Args:
            obj: The model obj.
            registered_on: The transaction the write runs in.
            current_span: The savepoint span the write runs in.

        Returns:
            The layer, and whether it is new - its rollback and commit callbacks are then still to
            be registered.
        """
        stack = obj._pending_rollback_restore_stack or []
        stack[:] = [entry for entry in stack if not entry.client.transaction_state.finished]
        for entry in reversed(stack):
            if entry.client is registered_on and entry.span is current_span:
                return entry, False
        layer = RollbackRestoreLayer(registered_on, current_span, InstanceSnapshot(obj))
        stack.append(layer)
        object.__setattr__(obj, "_pending_rollback_restore_stack", stack)
        return layer, True

    @staticmethod
    def take_rollback_snapshots(obj: Model, layer: RollbackRestoreLayer) -> list[InstanceSnapshot]:
        """Detach a layer and later layers of the same transaction in capture order.

        Released savepoints still belong to their outer transaction. Its rollback must
        undo their snapshots before its earlier snapshot; their later callbacks become
        no-ops. Detaching first also prevents a failing setter from leaving stale layers.

        Args:
            obj: The model obj.
            layer: The layer.
        """
        stack = obj._pending_rollback_restore_stack or []
        index = next((index for index, entry in enumerate(stack) if entry is layer), None)
        if index is None:
            # Restored already, by the rollback of the transaction its savepoint was released into.
            return []
        rolled_back = [entry for entry in stack[index:] if entry.client is layer.client]
        for entry in rolled_back:
            RollbackRestores.pop_pending_rollback_restore_layer(obj, entry)
        return [entry.snapshot for entry in rolled_back]
