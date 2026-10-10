from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.dialects.base.transactions.savepoints.savepoint_span import current_savepoint_span
from hare.fields.constants import ROLLBACK_RESTORE_UNSET
from hare.transactions.atomic.atomic import Atomic
from hare.transactions.transactions import Transactions

if TYPE_CHECKING:  # pragma: nocoverage
    from collections.abc import Iterable

    from hare.dialects.base.client.database_client import DatabaseClient
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
        if not using.is_transaction_client:
            return
        registered_on = Atomic.get_connection(using.connection_alias)
        layer, is_new_layer = RollbackRestores.get_rollback_restore_layer(
            obj, registered_on, current_savepoint_span.get()
        )
        if is_new_layer:

            def _on_rollback() -> None:
                RollbackRestores.restore_rollback_layer(obj, layer)

            def _on_commit() -> None:
                RollbackRestores.pop_pending_rollback_restore_layer(obj, layer)

            Transactions.on_rollback(_on_rollback, using=using.connection_alias)
            Transactions.on_commit(_on_commit, using=using.connection_alias)
        if field_name not in layer["pending"]:
            layer["pending"][field_name] = old_value

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
        registered_on = Atomic.get_connection(using.connection_alias)
        current_span = current_savepoint_span.get()
        new_layers: list[tuple[Model, dict[str, Any]]] = []
        for instance, field_name, old_value in restores:
            layer, is_new_layer = RollbackRestores.get_rollback_restore_layer(instance, registered_on, current_span)
            if is_new_layer:
                new_layers.append((instance, layer))
            if field_name not in layer["pending"]:
                layer["pending"][field_name] = old_value
        if not new_layers:
            return

        def _on_rollback() -> None:
            for instance, layer in new_layers:
                RollbackRestores.restore_rollback_layer(instance, layer)

        def _on_commit() -> None:
            for instance, layer in new_layers:
                RollbackRestores.pop_pending_rollback_restore_layer(instance, layer)

        Transactions.on_rollback(_on_rollback, using=using.connection_alias)
        Transactions.on_commit(_on_commit, using=using.connection_alias)

    @staticmethod
    def pop_pending_rollback_restore_layer(obj: Model, layer: dict[str, Any]) -> None:
        """Removes ``layer`` from this obj's pending-rollback-restore stack - by identity: two
        layers can hold equal content.

        Args:
            obj: The model obj.
            layer: The layer to remove.
        """
        stack: list[dict[str, Any]] = obj._pending_rollback_restore_stack or []
        for index, entry in enumerate(stack):
            if entry is layer:
                del stack[index]
                return

    @staticmethod
    def get_rollback_restore_layer(
        obj: Model, registered_on: DatabaseClient, current_span: Any
    ) -> tuple[dict[str, Any], bool]:
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
        stack: list[dict[str, Any]] = obj._pending_rollback_restore_stack or []
        while stack and stack[-1]["client"]._finalized:
            stack.pop()
        if stack and stack[-1]["span"] is current_span:
            return stack[-1], False
        layer: dict[str, Any] = {"span": current_span, "client": registered_on, "pending": {}}
        stack.append(layer)
        object.__setattr__(obj, "_pending_rollback_restore_stack", stack)
        return layer, True

    @staticmethod
    def restore_rollback_layer(obj: Model, layer: dict[str, Any]) -> None:
        """Puts back the old values of a layer whose transaction or savepoint rolled back, and drops
        the layer.

        Args:
            obj: The model obj.
            layer: The layer.
        """
        # Local import: soft deletion registers its writes for a rollback restore here.
        from hare.models.deletion.soft_deletion import SoftDeletion

        RollbackRestores.pop_pending_rollback_restore_layer(obj, layer)
        for name, value in layer["pending"].items():
            if name == obj._meta.soft_delete_field:
                SoftDeletion.restore_soft_delete_field(
                    obj,
                    value is not ROLLBACK_RESTORE_UNSET,
                    None if value is ROLLBACK_RESTORE_UNSET else value,
                )
            else:
                RollbackRestores.restore_field_value(obj, name, value)

    @staticmethod
    def restore_field_value(obj: Model, field_name: str, old_value: Any) -> None:
        """Puts ``field_name`` back to what it held before a write that didn't take effect.

        Args:
            obj: The model obj.
            field_name: The attribute to restore.
            old_value: The previous value, or ``ROLLBACK_RESTORE_UNSET`` if the attribute wasn't
                loaded at all (a ``.only()``/``.defer()`` obj) - it goes back to unloaded
                instead of gaining a made-up value.
        """
        if old_value is ROLLBACK_RESTORE_UNSET:
            if hasattr(obj, field_name):
                object.__delattr__(obj, field_name)
        else:
            setattr(obj, field_name, old_value)
